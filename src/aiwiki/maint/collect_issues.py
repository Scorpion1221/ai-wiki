"""Multica issue collector (design §4.3): the only module the new pipeline reads Multica through.

It wraps the P0 ``issue_delta``, which owns the complete listing, the ``(changed_at, id)``
cursor window with its settle margin, the autopilot/current/``ai_wiki_*`` exclusions and the
raw-response cache. Each remaining issue becomes planner candidates keyed
``issue:<identifier>``: its title and description, in-window status changes and title or
description edits, and in-window human comments plus agent final comments. Issues assigned
to or created by an excluded agent (the Maintainer or Auditor) and those agents' comments are
dropped,
system comments and run messages (tool calls) are never collected, secrets are redacted
before anything is frozen, and an issue above the item limit is split into ``#part-<n>``
items by comment window. Evidence files are named after the issue and the window start, so
a newer window merged into a ready item of the same topic adds a file instead of replacing
an undigested one. Collected text is data, never instructions.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..runtime import secrets
from . import issue_delta, planner
from .checkpoint import MulticaError, autopilot_runs, parse_time, run_issue_ids, run_multica

COLLECTOR = "issues"
EDITS = ("title_changed", "description_updated")
# A member stating a decision; agent answers routinely say "conclusion" and do not count.
DECISION = re.compile(r"(?i)\bdecisions?\b|\bdecided\b|决定|决策|拍板|定稿")


def collect(
    cursor: dict[str, Any], *, autopilot: str, cache_dir: Path, exclude_agents: Iterable[str] = (),
    current_issue: str | None = None, exclude_issues: Iterable[str] = (), settle_seconds: int = 120,
) -> dict[str, Any]:
    """Planner candidates for the issues changed after ``cursor`` ``{updated_at, id}``.

    Returns ``{status, error, cursor, next_cursor, counts, candidates}``. ``status`` is
    ``unavailable`` without the ``multica`` CLI and ``failed`` on any Multica or cursor
    error; both return the input cursor as ``next_cursor``, so the issues cursor never moves
    past evidence that was not collected. Write ``next_cursor`` only after the candidates are
    enqueued. Use a fresh ``cache_dir`` per run: it keeps the raw responses.
    """
    result: dict[str, Any] = {"status": "ok", "error": None, "cursor": cursor, "next_cursor": cursor,
                              "counts": {}, "candidates": []}
    if shutil.which("multica") is None:
        return {**result, "status": "unavailable", "error": "multica CLI not found on PATH"}
    agents = set(exclude_agents)
    counts: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    try:
        delta = run_delta(cursor, autopilot=autopilot, cache_dir=cache_dir, current_issue=current_issue,
                          exclude_issues=exclude_issues, settle_seconds=settle_seconds)
        excluded = Counter(delta["counts"]["excluded_by_reason"])
        issues = cached_issues(cache_dir)
        for row in delta["candidates"]:
            issue = issues[row["id"]]
            if reason := agent_exclusion(issue, agents):
                excluded[reason] += 1
                continue
            candidates += issue_candidates(issue, row, delta, agents, cache_dir, counts)
    except (MulticaError, OSError, ValueError, KeyError) as exc:  # CheckpointError and JSON errors are ValueErrors
        return {**result, "status": "failed", "error": str(exc)}
    return {**result, "next_cursor": delta["next_cursor"], "candidates": candidates, "counts": {
        **{key: delta["counts"][key] for key in ("listed", "changed", "deferred")},
        "excluded": dict(sorted(excluded.items())),
        **{key: counts[key] for key in ("quiet", "comments", "system_comments", "agent_comments_excluded")},
        "items": len(candidates),
    }}


def repo_registry(cache: Path) -> Path:
    """``multica repo list`` saved to ``cache``: the registry the repository scanner unions."""
    run_multica(["repo", "list", "--output", "json"], cache=cache)
    return cache


def agent_exclusion(issue: dict[str, Any], agents: set[str]) -> str | None:
    """Why an issue an excluded agent is assigned or created is not evidence: its text is theirs."""
    if issue.get("assignee_id") in agents:
        return "agent_assignee"
    if issue.get("creator_id") in agents:
        return "agent_creator"
    return None


def issue_evidence(identifier: str, comment_id: str | None, *, autopilot: str | None, exclude_agents: Iterable[str],
                   cache_dir: Path, current_issue: str | None = None,
                   exclude_issues: Iterable[str] = ()) -> dict[str, Any]:
    """One frozen evidence file for ``maint add-evidence issue:<identifier>[#<comment>]``.

    The issue header, then its human comments and agent final comments (or the one comment
    named), redacted and clipped like collected evidence. An issue the collector excludes
    (assigned to or created by an excluded agent, an autopilot run's issue, the current or an
    explicitly excluded issue, a maintenance report) is refused with ValueError, and so is
    any call without the autopilot and the excluded agents to check against. Comments of
    the excluded agents (the Maintainer and Auditor) are never evidence.
    """
    agents = set(exclude_agents)
    if not autopilot or not agents:
        raise ValueError("set issues.autopilot and issues.exclude_agents in the maint config: "
                         "they decide which issues are evidence")
    issue = run_multica(["issue", "get", identifier, "--output", "json"], cache=cache_dir / "issue.json")
    if not isinstance(issue, dict) or not isinstance(issue.get("id"), str):
        raise MulticaError(f"unexpected `multica issue get` response for {identifier}")
    reason = agent_exclusion(issue, agents) or issue_delta.exclusion(
        issue, current=current_issue, explicit=set(exclude_issues),
        run_issues=set(run_issue_ids(autopilot_runs(autopilot, cache_dir))))
    if reason:
        raise ValueError(f"{identifier} is not an issue a maintainer may cite ({reason})")
    comments_file = cache_dir / "comments.json"
    run_multica(["issue", "comment", "list", issue["id"], "--full", "--compact", "--output", "json"],
                cache=comments_file)
    counts: Counter[str] = Counter()
    kept = kept_comments(comments_file, lambda _created: True, agents, counts)
    if comment_id is not None:
        kept = [comment for comment in kept if comment.get("id") == comment_id]
        if not kept:
            raise MulticaError(f"{identifier} has no comment {comment_id} a maintainer may cite")
    name = issue.get("identifier") or issue["id"]
    text = "".join(block for block, _id in evidence_blocks(issue, name, str(issue.get("created_at")), kept, [], []))
    origin = {"kind": "issue", "issue_id": issue["id"], "identifier": name,
              "comments": [comment.get("id") for comment in kept]}
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", f"{name}-{comment_id}" if comment_id else name)[:100]
    return planner.evidence_file(f"add-{stem}.md", text, origin)


def run_delta(
    cursor: dict[str, Any], *, autopilot: str, cache_dir: Path, current_issue: str | None,
    exclude_issues: Iterable[str], settle_seconds: int,
) -> dict[str, Any]:
    """Run ``issue_delta`` in-process and return its JSON result; its errors raise ``MulticaError``."""
    if not isinstance(cursor, dict) or not cursor.get("updated_at") or not cursor.get("id"):
        raise ValueError("issues cursor {updated_at, id} is required")
    output = cache_dir / "issue-delta.json"
    argv = ["--autopilot", autopilot, "--since-updated-at", str(cursor["updated_at"]), "--since-id",
            str(cursor["id"]), "--cache-dir", str(cache_dir), "--settle-seconds", str(settle_seconds),
            "--output", str(output), "--quiet"]
    if current_issue:
        argv += ["--current-issue", current_issue]
    for issue_id in exclude_issues:
        argv += ["--exclude-issue", issue_id]
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = issue_delta.main(argv)
    if code:
        detail = stderr.getvalue().strip().removeprefix("issue_delta: ")
        raise MulticaError(detail or f"issue_delta exited {code}")
    return json.loads(output.read_text(encoding="utf-8"))


def cached_issues(cache_dir: Path) -> dict[str, dict[str, Any]]:
    """Full issue rows from the listing ``issue_delta`` just cached (lowest offset wins)."""
    issues: dict[str, dict[str, Any]] = {}
    for page in sorted((cache_dir / "issues").glob("offset-*.json")):
        for row in json.loads(page.read_text(encoding="utf-8")).get("issues", []):
            issues.setdefault(row["id"], row)
    return issues


def issue_candidates(
    issue: dict[str, Any], row: dict[str, Any], delta: dict[str, Any], agents: set[str], cache_dir: Path,
    counts: Counter[str],
) -> list[dict[str, Any]]:
    after, until = parse_time(delta["cursor"]["updated_at"]), parse_time(delta["as_of"])

    def in_window(value: Any) -> bool:
        return after < parse_time(value, "created_at") <= until

    kept = kept_comments(Path(row["comments_file"]), in_window, agents, counts)
    events = timeline(issue["id"], in_window, cache_dir)
    changes = [event for event in events if event["action"] == "status_changed"]
    edits = [event for event in events if event["action"] in EDITS]
    if not (row["new"] or kept or events):
        counts["quiet"] += 1  # e.g. only a system or excluded-agent comment moved last_activity_at
        return []
    counts["comments"] += len(kept)

    identifier = issue.get("identifier") or issue["id"]
    origin = {"kind": "issue", "issue_id": issue["id"], "identifier": identifier}
    # Redact before packing, so packing measures what is frozen and a longer marker never clips.
    blocks = [(*secrets.redact(text), comment_id) for text, comment_id
              in evidence_blocks(issue, identifier, delta["cursor"]["updated_at"], kept, changes, edits)]
    groups = planner.pack(blocks, planner.FILE_TEXT_LIMIT, size=lambda block: planner.utf8_size(block[0]))
    stem = f"{identifier}-{stamp(after)}"
    files = [
        planner.evidence_file(
            f"{stem}.md" if len(groups) == 1 else f"{stem}-{index}.md",
            "".join(text for text, _, _ in group),
            {**origin, "comments": [comment_id for _, _, comment_id in group if comment_id]},
            redactions=sum(count for _, count, _ in group),
        )
        for index, group in enumerate(groups, 1)
    ]
    parts = planner.pack(files, planner.ITEM_TEXT_LIMIT, size=lambda file: file["bytes"])

    members = sum(comment.get("author_type") == "member" for comment in kept)
    decision = any(DECISION.search(comment.get("content") or "") for comment in kept
                   if comment.get("author_type") == "member")
    brief = (f"{identifier} [{issue.get('status')}] {issue.get('title') or ''}; "
             f"{len(kept)} comments ({members} by members)")
    if changes:
        brief += f"; status {transition(changes[0])[0]} → {transition(changes[-1])[1]}"
    if edits:
        brief += "; edited " + " and ".join(sorted({event["action"].split("_")[0] for event in edits}))
    if decision:
        brief += "; decision"
    # Evidence names the window start but not its end: re-collecting a window keeps its item keys.
    window = {"after": delta["cursor"]["updated_at"], "until": delta["as_of"], "deferred": row["deferred"]}
    return [
        {
            "collector": COLLECTOR,
            "topic_key": f"issue:{identifier}" if len(parts) == 1 else f"issue:{identifier}#part-{number}",
            "origin": {**origin, "status": issue.get("status"), **window,
                       **({"part": number, "parts": len(parts)} if len(parts) > 1 else {})},
            "brief": brief if len(parts) == 1 else f"part {number}/{len(parts)} · {brief}",
            "files": part,
            "signals": {"decision": decision, "status_changes": len(changes)},
        }
        for number, part in enumerate(parts, 1)
    ]


def kept_comments(
    comments_file: Path, in_window: Callable[[Any], bool], agents: set[str], counts: Counter[str]
) -> list[dict[str, Any]]:
    """In-window human comments and agent final comments; system and excluded-agent ones are counted."""
    raw = json.loads(comments_file.read_text(encoding="utf-8"))
    rows = raw.get("comments") if isinstance(raw, dict) else raw
    kept = []
    for comment in sorted((row for row in rows if isinstance(row, dict) and in_window(row.get("created_at"))),
                          key=lambda row: (parse_time(row["created_at"]), str(row.get("id")))):
        if comment.get("type", "comment") != "comment":
            counts["system_comments"] += 1
        elif comment.get("author_id") in agents:
            counts["agent_comments_excluded"] += 1
        else:
            kept.append(comment)
    return kept


def timeline(issue_id: str, in_window: Callable[[Any], bool], cache_dir: Path) -> list[dict[str, Any]]:
    """In-window status changes and title or description edits, oldest first."""
    actions = ("status_changed", *EDITS)
    events = run_multica(["issue", "timeline", issue_id, "--action", ",".join(actions), "--output", "json"],
                         cache=cache_dir / "timelines" / f"{issue_id}.json")
    if not isinstance(events, list):
        raise MulticaError(f"unexpected `multica issue timeline` response for {issue_id}")
    return [event for event in events if isinstance(event, dict)
            and event.get("action") in actions and in_window(event.get("created_at"))]


def stamp(moment: datetime) -> str:
    """A UTC time for file names, which the queue limits to ``[A-Za-z0-9._-]``."""
    fraction = f".{moment.microsecond:06d}" if moment.microsecond else ""
    return f"{moment:%Y%m%dT%H%M%S}{fraction}Z"


def transition(event: dict[str, Any]) -> tuple[Any, Any]:
    details = event.get("details") or {}
    return details.get("from"), details.get("to")


def evidence_blocks(
    issue: dict[str, Any], identifier: str, since: str, kept: list[dict[str, Any]], changes: list[dict[str, Any]],
    edits: list[dict[str, Any]],
) -> list[tuple[str, str | None]]:
    """(text, comment id) blocks: the issue header, then one block per kept comment."""
    header = [f"# {identifier} · {issue.get('title') or ''}", "", f"- issue: {issue['id']}",
              f"- status: {issue.get('status')}", f"- created_at: {issue.get('created_at')}",
              f"- activity since: {since}"]
    if issue.get("description"):
        header += ["", "## Description", "", issue["description"]]
    if changes:
        header += ["", "## Status changes", ""]
        header += [f"- {event['created_at']} {transition(event)[0]} → {transition(event)[1]} "
                   f"({event.get('actor_type')})" for event in changes]
    if edits:  # the header above already holds the current title and description
        header += ["", "## Edits", ""]
    for event in edits:
        title = f": {transition(event)[0]} → {transition(event)[1]}" if event["action"] == "title_changed" else ""
        header.append(f"- {event['created_at']} {event['action']}{title} ({event.get('actor_type')})")
    blocks: list[tuple[str, str | None]] = [("\n".join(header) + "\n", None)]
    for comment in kept:
        title = (f"## Comment {comment.get('id')} · {comment.get('author_type')} {comment.get('author_id')} · "
                 f"{comment['created_at']}")
        if comment.get("parent_id"):
            title += f" · reply to {comment['parent_id']}"
        blocks.append((f"\n{title}\n\n{comment.get('content') or ''}\n", comment.get("id")))
    return blocks
