#!/usr/bin/env python3
"""List Multica issues changed after the checkpoint cursor, read-only and deterministic.

An issue changed when max(updated_at, last_activity_at) is after the cursor; comparison is
on the (changed_at, id) tuple. Comment-only activity moves last_activity_at, not
updated_at. ``next_cursor`` only passes changes up to ``as_of`` = listing start minus
``--settle-seconds``, so changes made while the paginated listing runs are picked up by the
next run instead of being skipped by a cursor that already passed them. An issue changed
after ``as_of`` is still a candidate now (``deferred``: true) with its comments up to
``as_of``: the next run lists it again but fetches comments only after ``next_cursor``, so
its older comments would otherwise never surface. Such overlap may repeat a comment.

The listing pages by created_at with one row of overlap and must match the server's total;
a shifted page (an issue deleted mid-listing) or a count mismatch fails instead of
silently skipping issues.

Maintenance issues are excluded: the current issue, every issue created by the autopilot's
runs, ``--exclude-issue`` ids, and issues carrying ``ai_wiki_*`` metadata (checkpoints and
recovery receipts). Comments are fetched only for candidates. Raw responses are cached in
``--cache-dir``; the output keeps compact excerpts plus ``next_cursor`` for checkpoint
``build``. Exit codes: 0 ok; 2 usage or Multica failure (do not advance the issues cursor).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .checkpoint import (
    CheckpointError,
    MulticaError,
    autopilot_runs,
    format_time,
    issues_cursor,
    load_json,
    parse_time,
    run_issue_ids,
    run_multica,
)

MAINTENANCE_METADATA_PREFIX = "ai_wiki_"
MAX_PAGES = 1000


def clip(text: Any, limit: int) -> str:
    value = text if isinstance(text, str) else ""
    return value if len(value) <= limit else value[:limit] + "…"


def list_issues(cache_dir: Path, page_size: int) -> list[dict[str, Any]]:
    # created_at is immutable, so offset pages stay stable while issues are being edited.
    # The server caps a page (100 rows) whatever --limit says, so advance by the rows it
    # returned. Each page re-reads the previous page's last row: a different row there means
    # the order shifted (a deletion) and an issue may have been skipped.
    issues: dict[str, dict[str, Any]] = {}
    offset, overlap = 0, None
    for _ in range(MAX_PAGES):
        response = run_multica(
            ["issue", "list", "--sort", "created_at", "--direction", "asc",
             "--limit", str(page_size), "--offset", str(offset), "--output", "json"],
            cache=cache_dir / "issues" / f"offset-{offset:06d}.json",
        )
        rows = response.get("issues") if isinstance(response, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get("id"), str)
                                                 for row in rows):
            raise MulticaError("unexpected `multica issue list` response")
        if overlap is not None and (not rows or rows[0]["id"] != overlap):
            raise MulticaError(f"issue list shifted at offset {offset} while paging; retry later")
        for row in rows:
            issues[row["id"]] = row
        if not response.get("has_more", len(rows) >= page_size):
            total = response.get("total")
            if isinstance(total, int) and total != len(issues):
                raise MulticaError(f"issue list returned {len(issues)} unique issues but total is {total}")
            return list(issues.values())
        if len(rows) < 2:
            raise MulticaError(f"issue list returned {len(rows)} row(s) at offset {offset} with has_more")
        offset += len(rows) - 1
        overlap = rows[-1]["id"]
    raise MulticaError(f"issue list exceeded {MAX_PAGES} pages")


def changed_at(issue: dict[str, Any]) -> datetime:
    updated = parse_time(issue.get("updated_at"), f"{issue.get('identifier') or issue['id']}.updated_at")
    activity = issue.get("last_activity_at")
    return max(updated, parse_time(activity, "last_activity_at")) if activity else updated


def exclusion(issue: dict[str, Any], *, current: str | None, run_issues: set[str], explicit: set[str]) -> str | None:
    if issue["id"] == current:
        return "current_issue"
    if issue["id"] in run_issues:
        return "autopilot_run"
    if issue["id"] in explicit:
        return "explicit"
    metadata = issue.get("metadata")
    if isinstance(metadata, dict) and any(str(key).startswith(MAINTENANCE_METADATA_PREFIX) for key in metadata):
        return "maintenance_metadata"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--autopilot", required=True, help="autopilot id whose run issues are excluded")
    parser.add_argument("--cursor-json", type=Path, help="checkpoint, checkpoint find output, or prior delta")
    parser.add_argument("--since-updated-at", help="explicit cursor timestamp")
    parser.add_argument("--since-id", help="explicit cursor issue id")
    parser.add_argument("--current-issue", default=os.environ.get("MULTICA_ISSUE_ID"),
                        help="maintenance issue running now (default: $MULTICA_ISSUE_ID)")
    parser.add_argument("--exclude-issue", action="append", default=[], help="another issue to exclude")
    parser.add_argument("--cache-dir", type=Path, required=True, help="store raw multica responses here")
    parser.add_argument("--settle-seconds", type=int, default=120, help="leave the newest changes to the next run")
    parser.add_argument("--max-chars", type=int, default=300, help="excerpt length for descriptions and comments")
    parser.add_argument("--max-comments", type=int, default=8,
                        help="latest in-window comments excerpted per candidate (all stay in comments_file)")
    parser.add_argument("--page-size", type=int, default=100, help="rows per issue list call (the server caps it)")
    parser.add_argument("--output", type=Path, help="write the JSON result here as well as stdout")
    parser.add_argument("--quiet", action="store_true", help="write only --output; suppress JSON on stdout")
    args = parser.parse_args(argv)
    if bool(args.cursor_json) == bool(args.since_updated_at or args.since_id):
        parser.error("give either --cursor-json or --since-updated-at with --since-id")
    if args.quiet and not args.output:
        parser.error("--quiet requires --output")
    if args.page_size < 2 or min(args.settle_seconds, args.max_chars, args.max_comments) < 0:
        parser.error("--page-size must be at least 2; --settle-seconds, --max-chars, --max-comments non-negative")
    try:
        return run(args)
    except (ValueError, MulticaError, OSError) as exc:  # CheckpointError and JSON errors are ValueErrors
        print(f"issue_delta: {exc}", file=sys.stderr)
        return 2


def run(args: argparse.Namespace) -> int:
    if args.cursor_json:
        cursor = issues_cursor(load_json(args.cursor_json))
    else:
        cursor = {"updated_at": args.since_updated_at, "id": args.since_id}
    if not isinstance(cursor.get("id"), str) or not cursor["id"]:
        raise CheckpointError("cursor id is required")
    cursor_key = (parse_time(cursor.get("updated_at"), "cursor updated_at"), cursor["id"])

    started = datetime.now(UTC)
    as_of = started.replace(microsecond=0) - timedelta(seconds=args.settle_seconds)
    run_issues = set(run_issue_ids(autopilot_runs(args.autopilot, args.cache_dir)))
    issues = list_issues(args.cache_dir, args.page_size)

    window: list[tuple[tuple[datetime, str], dict[str, Any]]] = []
    for issue in issues:
        key = (changed_at(issue), issue["id"])
        if key > cursor_key:
            window.append((key, issue))
    window.sort(key=lambda item: item[0])
    # Only settled changes move the cursor; deferred issues are reported now and listed again.
    settled = [key for key, _ in window if key[0] <= as_of]

    explicit = set(args.exclude_issue)
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    comment_count = 0
    since = format_time(cursor_key[0].replace(microsecond=0))
    for key, issue in window:
        reason = exclusion(issue, current=args.current_issue, run_issues=run_issues, explicit=explicit)
        if reason:
            excluded.append({"id": issue["id"], "identifier": issue.get("identifier"), "reason": reason})
            continue
        comments_file = args.cache_dir / "comments" / f"{issue['id']}.json"
        response = run_multica(
            ["issue", "comment", "list", issue["id"], "--since", since, "--compact", "--output", "json"],
            cache=comments_file,
        )
        rows = response.get("comments") if isinstance(response, dict) else response
        if not isinstance(rows, list):
            raise MulticaError(f"unexpected `multica issue comment list` response for {issue['id']}")
        comments = []
        for comment in rows:
            if not isinstance(comment, dict):
                continue
            created = parse_time(comment.get("created_at"), "comment created_at")
            if not cursor_key[0] < created <= as_of:
                continue
            content = comment.get("content")
            comments.append({
                "id": comment.get("id"),
                "parent_id": comment.get("parent_id"),
                "author_type": comment.get("author_type"),
                "author_id": comment.get("author_id"),
                "created_at": comment.get("created_at"),
                "chars": len(content) if isinstance(content, str) else 0,
                "excerpt": clip(content, args.max_chars),
            })
        comment_count += len(comments)
        comments.sort(key=lambda row: parse_time(row["created_at"]))
        shown = comments[-args.max_comments:] if args.max_comments else []
        description = issue.get("description")
        created_at = issue.get("created_at")
        candidates.append({
            "id": issue["id"],
            "identifier": issue.get("identifier"),
            "title": issue.get("title"),
            "status": issue.get("status"),
            "parent_issue_id": issue.get("parent_issue_id"),
            "assignee_type": issue.get("assignee_type"),
            "created_at": created_at,
            "updated_at": issue.get("updated_at"),
            "last_activity_at": issue.get("last_activity_at"),
            "changed_at": format_time(key[0]),
            "deferred": key[0] > as_of,
            "new": bool(created_at) and parse_time(created_at, "created_at") > cursor_key[0],
            "description_chars": len(description) if isinstance(description, str) else 0,
            "description_excerpt": clip(description, args.max_chars),
            "comments_in_window": len(comments),
            "comments_omitted": len(comments) - len(shown),
            "comments": shown,
            "comments_file": str(comments_file),
        })

    # Excluded issues are advanced over too: they never become candidates.
    next_cursor = {"updated_at": format_time(settled[-1][0]), "id": settled[-1][1]} if settled else cursor
    reasons = Counter(row["reason"] for row in excluded)
    result = {
        "version": 1,
        "generated_at": format_time(started.replace(microsecond=0)),
        "as_of": format_time(as_of),
        "cursor": cursor,
        "next_cursor": next_cursor,
        "counts": {
            "listed": len(issues),
            "changed": len(window),
            "candidates": len(candidates),
            "excluded": len(excluded),
            "excluded_by_reason": dict(sorted(reasons.items())),
            "deferred": len(window) - len(settled),
            "comments": comment_count,
        },
        "excluded": excluded,
        "candidates": candidates,
        "cache_dir": str(args.cache_dir),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if not args.quiet:
        sys.stdout.write(rendered)
    counts = " ".join(f"{key}={value}" for key, value in result["counts"].items() if not isinstance(value, dict))
    print(f"issue_delta: {counts} next_cursor={next_cursor['updated_at']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
