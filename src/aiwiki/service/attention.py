"""Attention work items: the concepts nobody is fixing (design §5.3, W18). Deterministic, no LLM.

Two states reach no work item otherwise. An auditor's ``unverified`` verdict leaves a concept
stable, unverified in its current version and off the audit backlog until a new generation or
a push changes it; and a concept whose effective freshness elapsed (``engine.document``: a
declared ``stale_after``, else 90 days after its generation) still reads stable to every
consumer. When a maintainer run starts, ``maint_state.acquire_lease`` queues one item per such
concept through the ordinary enqueue path, from the specs ``plan`` returns.

Each item carries exactly one file, ``notice.md``, whose bytes depend on (path, content hash,
reason, review id) and, for ``stale``, the day freshness elapsed: never a collection time. So a
repeated run start is a duplicate (a notice the maintainer skipped stays skipped), a new version
of the concept is a new item, a concept that goes stale again after a new generation of the same
content is too, and a waiting item of the same topic absorbs the newer notice. The notice is not
evidence: its origin kind is ``attention``, and the curate gate refuses a packet citing one.
"""
from __future__ import annotations

import base64
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from ..engine.document import (
    BASIS_DECLARED,
    DEFAULT_STALE_DAYS,
    OKFDocumentError,
    _instant,
    freshness_basis,
    parse_document,
    stale_date,
)
from ..maint import planner
from ..runtime import changeset
from ..runtime.audit import _cited, _concepts  # the backlog's own walk and citation rule
from . import ingest as I

KIND = changeset.NOTICE_KIND  # origin.kind of the item and of its notice file: the evidence gate keys on it
NOTICE = "notice.md"
AUDIT_UNVERIFIED, STALE = "audit_unverified", "stale"
LEDGER = "sources/.hashes.yaml"


def today() -> date:
    """The day a run start judges freshness on: the one every read surface uses; tests pin it."""
    return date.today()


def ledger(bundle: Path) -> dict[str, str]:
    """``sources/.hashes.yaml``, ``{sources/<name>: sha256}``: the record ``runtime.audit._frozen``
    trusts. A file's presence never makes it frozen evidence; its entry here does."""
    try:
        value = yaml.safe_load((bundle / LEDGER).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    return {str(key): str(sha) for key, sha in value.items()} if isinstance(value, dict) else {}


def unverified_reviews(bundle: Path) -> dict[tuple[str, str], dict]:
    """``(path, content hash)`` -> the newest review of those exact bytes, when it ended unverified.

    Only done audit changesets an admin did not revert count, as for the backlog (§5.3), and
    only the gate's ``outcome``: whatever verdict was asked for, ``unverified`` is what the
    service concluded about that version. A later review of the same bytes replaces an earlier
    one, so a later ``verified`` clears it.
    """
    reverted = I.reverted_by(bundle)
    jobs = sorted((job for job in I.changeset_jobs(bundle) if job.get("kind") == "audit"
                   and job.get("status") == "done" and job.get("id") not in reverted),
                  key=lambda job: (str(job.get("finished") or job.get("created") or ""), str(job.get("id"))))
    newest: dict[tuple[str, str], dict] = {}
    for job in jobs:
        for review in job.get("reviews") or []:
            if isinstance(review, dict) and isinstance(review.get("path"), str) \
                    and isinstance(review.get("content_hash"), str):
                newest[(review["path"], review["content_hash"])] = {
                    **review, "review": str(job.get("id")), "at": job.get("finished") or job.get("created")}
    return {key: review for key, review in newest.items() if review.get("outcome") == "unverified"}


def _notice(rel: str, base: str, reason: str, frontmatter: dict, review: dict | None, sources: list[str]) -> bytes:
    """What is wrong, the auditor's words, the cited sources and what to do; no time but the review's."""
    lines = [f"# Attention: {rel}", "", f"- concept: `{rel}`", f"- content hash: `{base}`", f"- reason: {reason}"]
    if review is not None:
        lines += [f"- review: {review['review']} at {review.get('at')}",
                  f"- downgrade: {review.get('downgrade') or 'none (the auditor judged it unverified)'}", "",
                  "The auditor judged this exact version unsupported by the evidence it cites. It stays",
                  "stable but unverified, and no audit looks at it again until its content changes.", "",
                  "The auditor's note:", ""]
        lines += [f"> {line}" if line.strip() else ">" for line in str(review.get("note") or "(none)").splitlines()]
    elif freshness_basis(frontmatter) == BASIS_DECLARED:
        lines += ["", f"Its declared `stale_after` ({stale_date(frontmatter).isoformat()}) has passed: its claims",
                  "may no longer hold."]
    else:
        lines += ["", f"It declares no `stale_after`, and its generation is {DEFAULT_STALE_DAYS} or more days old",
                  f"(the default freshness; stale since {stale_date(frontmatter).isoformat()}): its claims may",
                  "no longer hold."]
    lines += ["", "Cited sources:", ""]
    lines += [f"- `{source}`" for source in sources] or ["- none of the bundle's sources/"]
    lines += ["", "What to do:", "",
              "1. Gather evidence: `ai-wiki maint add-evidence <item> source:sources/<file>` freezes a source",
              "   the bundle already holds (one cited above, or a newer one); a Git file or an issue works too.",
              "2. Fix the claims the evidence does not support, cite it, then `ai-wiki propose`."
              + (" If they all still hold, set `stale_after` to when they should next be checked."
                 if review is None else ""),
              "3. Or skip it with a reason: `ai-wiki maint skip <item> --reason insufficient_evidence`.", "",
              "This notice is not evidence: a changeset citing it is refused. Add evidence first.", ""]
    return "\n".join(lines).encode("utf-8")


def plan(bundle: Path, *, today: date, tree: Path | None = None) -> list[dict]:
    """The attention items due now as ``POST /maint/items`` specs, oldest first.

    ``tree`` is the checkout read (the bundle by default); the audit receipts are the bundle's.
    A deprecated concept, or one that does not parse, is skipped. An unverified review outranks
    elapsed freshness: one item per concept. Oldest is when the reason arose: the review's
    time, or the day freshness elapsed.
    """
    tree = tree or bundle
    unverified = unverified_reviews(bundle)
    found = []
    for rel, text in _concepts(tree):
        try:
            frontmatter = parse_document(text).frontmatter
        except OKFDocumentError:
            continue  # its curator fixes the YAML first
        if frontmatter.get("status") == "deprecated":
            continue
        base = changeset.content_hash(text)
        review = unverified.get((rel, base))
        deadline = stale_date(frontmatter)
        if review is not None:
            since = _instant(review.get("at"))
            origin = {"kind": KIND, "path": rel, "reason": AUDIT_UNVERIFIED, "base": base,
                      "review": review["review"], "reviewed_at": review.get("at"),
                      "downgrade": review.get("downgrade"), "note": review.get("note")}
            brief = f"{rel}: the auditor judged this version unverified" + (
                f" ({review['downgrade']})" if review.get("downgrade") else "")
        elif deadline is not None and today >= deadline:
            since = datetime(deadline.year, deadline.month, deadline.day, tzinfo=UTC)
            origin = {"kind": KIND, "path": rel, "reason": STALE, "base": base, "stale_on": deadline.isoformat(),
                      "basis": freshness_basis(frontmatter)}
            brief = f"{rel}: stale since {deadline.isoformat()}"
        else:
            continue
        notice = _notice(rel, base, origin["reason"], frontmatter, review, sorted(_cited(rel, frontmatter)))
        found.append((since or datetime.min.replace(tzinfo=UTC), rel, {
            "origin": origin, "topic_key": f"{KIND}:{rel}", "brief": brief,
            "priority": planner.PRIORITY[KIND if review is not None else "refresh"],
            "files": [{"name": NOTICE, "content_b64": base64.b64encode(notice).decode(),
                       "origin": {"kind": KIND, "path": rel, "reason": origin["reason"]}}]}))
    found.sort(key=lambda row: (row[0], row[1]))
    return [spec for _since, _rel, spec in found]
