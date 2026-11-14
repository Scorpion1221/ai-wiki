"""Attention work items: the concepts nobody is fixing (design §5.3, W18). Deterministic, no LLM.

Two states reach no work item otherwise. An auditor's ``unverified`` verdict leaves a concept
stable, unverified in its current version and off the audit backlog until a new generation or
a push changes it; and a concept whose effective freshness elapsed (``engine.document``: a
declared ``stale_after``, else 90 days after its generation) still reads stable to every
consumer. When a maintainer run starts, ``maint_state.acquire_lease`` queues one item per such
concept through the ordinary enqueue path, from the specs ``plan`` returns.

Each item carries exactly one file, ``notice.md``, which holds only facts: path, content hash,
reason, the review (id, time, requested verdict, downgrade, note) or the stale date and its
basis, and the cited sources. Never a collection time and never guidance: what to do is the
item's ``brief`` (and the maintainer skill), which is not part of the item key. So a repeated
run start is a duplicate (a notice the maintainer skipped stays skipped, whatever the guidance
says by then), a new version of the concept is a new item, a concept that goes stale again
after a new generation of the same content is too, and a waiting item of the same topic absorbs
the newer notice. The notice is not evidence: its origin kind is ``attention``, and the curate
gate leaves it out of a packet and refuses a packet of nothing else.
"""
from __future__ import annotations

import base64
import re
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
AUDIT_UNVERIFIED, AUDIT_HELD, STALE, OVERSIZED = "audit_unverified", "audit_held", "stale", "oversized"
SPLITS_PER_RUN = 5  # oversized pages one run start queues for a split, the largest first
LEDGER = "sources/.hashes.yaml"
# What an auditor's note names that only the maintainer can change (a review restores title and
# status, and keeps stale_after unless it moves earlier): the brief's first words. 2026-11-05..07,
# 145 of 532 unverified notes named only these, and the maintainer had changed none of them.
FIXES = (
    (re.compile(r"(?<![小节])标题|\btitle\b", re.IGNORECASE), "title (--allow-retype)"),
    (re.compile(r"supersed|deprecat|退役|弃用|已被(?:取代|替代)|仍为?\s*stable|status:?\s*stable", re.IGNORECASE),
     "ended (--deprecate or --retire)"),
    (re.compile(r"stale_after", re.IGNORECASE), "stale_after (only earlier)"),
    (re.compile(r"\bdescription\b|摘要", re.IGNORECASE), "description (latest state)"),
)


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


# What each service downgrade (runtime.audit, design §5.4) means: the brief's words, never the
# notice's. A correction downgraded for adding (or for removing a hedge) is an edit the gate
# rejected, so its note describes what must not be applied without new evidence.
DOWNGRADES = {
    "D_NOVEL_TOKEN": "it added words or numbers no cited source holds",
    "D_NEW_LINK": "it added a link",
    "D_GROWTH": "it grew the concept instead of narrowing it",
    "D_CAVEAT_REMOVED": "it removed a caveat",
    "D_RESTORED": "it changed only what a review may not change",
    "D_INVALID_VERDICT": "the verdict was unusable",
    "D_NO_EVIDENCE": "the concept cites no frozen source",
}
REJECTED_EDITS = frozenset({"D_NOVEL_TOKEN", "D_NEW_LINK", "D_GROWTH", "D_CAVEAT_REMOVED"})


def _asked(verdict: object) -> str:
    return {"corrected": "correction", "verified": "verification"}.get(str(verdict), "verdict")


def _reason_lines(origin: dict) -> list[str]:
    """The fixed fact lines of one reason (``_notice``)."""
    if origin["reason"] == AUDIT_HELD:
        # A newer version of a concept the auditor refused version after version: it waits off the
        # audit backlog for the maintainer's answer, so the newest refusal's note says what to fix.
        lines = [f"- unverified versions in a row: {origin['refusals']}",
                 f"- last review: {origin['review']} at {origin.get('reviewed_at')}, of `{origin['reviewed_base']}`",
                 "- note:", ""]
        lines += [f"> {line}" if line.strip() else ">" for line in str(origin.get("note") or "(none)").splitlines()]
        return lines + [""]
    if origin["reason"] == AUDIT_UNVERIFIED:
        verdict, downgrade = origin.get("verdict"), origin.get("downgrade")
        lines = [f"- review: {origin['review']} at {origin.get('reviewed_at')}",
                 f"- requested verdict: {verdict}",
                 f"- outcome: unverified (the gate refused the requested {_asked(verdict)}: {downgrade})"
                 if downgrade else "- outcome: unverified (the auditor's own verdict)",
                 "- note, describing the edit the gate rejected:" if downgrade in REJECTED_EDITS else "- note:", ""]
        lines += [f"> {line}" if line.strip() else ">" for line in str(origin.get("note") or "(none)").splitlines()]
        return lines + [""]
    if origin["reason"] == OVERSIZED:
        return [f"- body: {origin['chars']} characters", f"- cap: {origin['cap']} characters"]
    return [f"- stale on: {origin['stale_on']}", f"- basis: {origin['basis']}"]


def _notice(rel: str, base: str, origin: dict, sources: list[str]) -> bytes:
    """Facts only, in fixed lines: these bytes are the item key, so no guidance lives here.
    A split that took the place of another reason (``also``) carries that reason's facts too."""
    lines = [f"# Attention: {rel}", "", f"- concept: `{rel}`", f"- content hash: `{base}`",
             f"- reason: {origin['reason']}"] + _reason_lines(origin)
    if origin.get("also"):
        lines += [f"- also due: {origin['also']['reason']}"] + _reason_lines(origin["also"])
    lines += ["- cited sources:"] + ([f"  - `{source}`" for source in sources] or ["  - none of the bundle's sources/"])
    return ("\n".join(lines) + "\n").encode("utf-8")


def fixes(note: object) -> list[str]:
    """The maintainer-only changes an auditor's note names, in ``FIXES`` order."""
    return [fix for pattern, fix in FIXES if isinstance(note, str) and pattern.search(note)]


def _brief(origin: dict) -> str:
    """What to do, front-loaded (``maint next`` shows 300 characters); not part of the item key."""
    tail = "cite it, propose. Else skip insufficient_evidence."
    if origin["reason"] == OVERSIZED and origin.get("also"):
        # Past 300 characters only after the split and the pointer to the earlier notice.
        return (f"Split: body {origin['chars']} chars, cap {origin['cap']}; notice.md also quotes a "
                f"{origin['also']['reason']} notice: answer it where each claim lands. Move dated readings to new "
                "concepts (concept new, citing sources) this page links; keep definition and latest state here "
                "(--allow-shrink). add-evidence: the cited files, propose. Nothing apart: answer in place.")
    if origin["reason"] == OVERSIZED:
        return (f"Split: body {origin['chars']} chars, cap {origin['cap']}. Move dated readings to new concepts "
                "(concept new, citing their sources) this page links; keep definition and latest state here "
                "(--allow-shrink). add-evidence source: the cited files, propose. Nothing apart: skip out_of_scope.")
    if origin["reason"] == STALE:
        why = "its declared stale_after" if origin["basis"] == BASIS_DECLARED else \
            f"{DEFAULT_STALE_DAYS} days after generation, the default"
        return (f"Stale since {origin['stale_on']} ({why}). Check the claims against evidence newer than the "
                f"generation (maint add-evidence: a Git file or an issue); fix what it contradicts, {tail}")
    downgrade = origin.get("downgrade")
    if not downgrade:
        named = fixes(origin.get("note"))
        lead = (f"Held off the audit until you answer ({origin.get('refusals')} versions unverified). "
                if origin["reason"] == AUDIT_HELD else "Unverified by the auditor. ")
        return (lead + (f"Fix first: {'; '.join(named)}. " if named else "notice.md quotes why. ")
                + "Add evidence (maint add-evidence, source:sources/<file> too); fix only what it shows, " + tail)
    rejected = " The note describes that rejected edit: never apply it without new evidence." \
        if downgrade in REJECTED_EDITS else ""
    return (f"The gate refused the auditor's {_asked(origin.get('verdict'))} ({downgrade}: "
            f"{DOWNGRADES.get(downgrade, 'a service downgrade')}).{rejected} Add evidence (maint add-evidence); "
            f"fix only what it shows, {tail}")


def plan(bundle: Path, *, today: date, tree: Path | None = None, held: list[dict] | None = None,
         body_chars: int | None = None) -> list[dict]:
    """The attention items due now as ``POST /maint/items`` specs, oldest first.

    ``tree`` is the checkout read (the bundle by default); the audit receipts are the bundle's.
    ``held`` is the audit backlog's ``held.concepts`` for that tree: each such version is an
    ``audit_held`` item carrying the newest refusal's note, so the maintainer answers what the
    auditor no longer re-reviews. A deprecated concept, or one that does not parse, is skipped.
    A review outranks elapsed freshness: one item per concept. Oldest is when the reason arose:
    the review's time, or the day freshness elapsed. Then, past ``body_chars`` (the gate's cap),
    the ``SPLITS_PER_RUN`` largest pages get an ``oversized`` split item instead, whatever other
    reason they have: its notice carries that reason's facts under ``also``. A page past the cap
    meets the same refusal on every review (2026-11-10: 1 of 16 passed, against 72% of the pages
    split since 0.3.25), and a fix that may not grow it never ends that loop.
    """
    tree = tree or bundle
    unverified = unverified_reviews(bundle)
    held = {entry["path"]: entry for entry in held or ()}
    due, large = {}, []  # path -> (since, base, frontmatter, body chars, origin or None, reviewed)
    for rel, text in _concepts(tree):
        try:
            document = parse_document(text)
        except OKFDocumentError:
            continue  # its curator fixes the YAML first
        frontmatter = document.frontmatter
        if frontmatter.get("status") == "deprecated":
            continue
        chars = len(document.body.strip())
        if body_chars and chars > body_chars:
            large.append((-chars, rel))
        base = changeset.content_hash(text)
        review = unverified.get((rel, base))
        deadline = stale_date(frontmatter)
        hold = held.get(rel)
        if review is None and hold is not None and hold["base"] == base:
            # The audit backlog holds this version (runtime.audit.backlog): it waits for an answer.
            last = hold["last"]
            since = _instant(last.get("at"))
            origin = {"kind": KIND, "path": rel, "reason": AUDIT_HELD, "base": base, "refusals": hold["refusals"],
                      "review": last["review"], "reviewed_at": last.get("at"), "reviewed_base": last["base"],
                      "note": last.get("note")}
            review = origin
        elif review is not None:
            since = _instant(review.get("at"))
            # ``verdict`` is what the auditor asked for; with a ``downgrade`` the gate refused it.
            origin = {"kind": KIND, "path": rel, "reason": AUDIT_UNVERIFIED, "base": base,
                      "review": review["review"], "reviewed_at": review.get("at"), "verdict": review.get("verdict"),
                      "downgrade": review.get("downgrade"), "note": review.get("note")}
        elif deadline is not None and today >= deadline:
            since = datetime(deadline.year, deadline.month, deadline.day, tzinfo=UTC)
            origin = {"kind": KIND, "path": rel, "reason": STALE, "base": base, "stale_on": deadline.isoformat(),
                      "basis": freshness_basis(frontmatter)}
        else:
            since, origin = None, None
        due[rel] = (since, base, frontmatter, chars, origin, review is not None)
    split = {rel for _size, rel in sorted(large)[:SPLITS_PER_RUN]}
    found, splits = [], []
    for rel, (since, base, frontmatter, chars, origin, reviewed) in due.items():
        if rel in split:
            other = origin
            origin = {"kind": KIND, "path": rel, "reason": OVERSIZED, "base": base, "chars": chars, "cap": body_chars}
            if other is not None:
                origin["also"] = {key: value for key, value in other.items() if key not in ("kind", "path", "base")}
        elif origin is None:
            continue
        notice = _notice(rel, base, origin, sorted(_cited(rel, frontmatter)))
        spec = {"origin": origin, "topic_key": f"{KIND}:{rel}", "brief": _brief(origin),
                "priority": planner.PRIORITY[KIND if reviewed and rel not in split else "refresh"],
                "files": [{"name": NOTICE, "content_b64": base64.b64encode(notice).decode(),
                           "origin": {"kind": KIND, "path": rel, "reason": origin["reason"]}}]}
        if rel in split:
            splits.append((-chars, rel, spec))
        else:
            found.append((since or datetime.min.replace(tzinfo=UTC), rel, spec))
    found.sort(key=lambda row: (row[0], row[1]))
    return [spec for _since, _rel, spec in found] + [spec for _size, _rel, spec in sorted(splits)]
