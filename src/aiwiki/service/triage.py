"""Triage review: the auditor re-checks the maintainer's skips and duplicates. Deterministic, no LLM.

The maintainer closes many collected items as ``skipped`` (no_durable_knowledge, out_of_scope,
insufficient_evidence) or ``duplicate`` (duplicate_of:<concept>). An adversarial re-check of the
first 0.3.5 run (2026-10-27) found durable knowledge lost in 32 of 37 such items, and a stricter
rule in the maintainer skill did not change that. So each decision gets a structural second
look: the auditor, another model, judges it against the item's own frozen files.

``backlog`` lists the items due: skipped or duplicate, closed by a maintainer run (``resolve``
under the lease: never an admin's hand close or the service's own close), at or after
``AIWIKI_TRIAGE_EPOCH``, holding at least one evidence file, and judged by no done review yet.
An item is judged once: a maintainer who closes a reopened item again must say why (its note),
and the auditor does not review that answer, so the two never loop. Without the epoch the
feature is off and the list is empty. A review is ``upheld`` (the item stays closed) or
``missed``: 1 to ``MAX_MISSED`` claims, each naming lines of one of the item's frozen evidence
files and the concept it belongs in. A missed review reopens the item as ready
(``maint_state.triage_reopen``) with ``triage.md`` attached: each claim with those lines quoted
verbatim, the auditor and the time, facts only. Like an attention notice it is never evidence
(origin kind ``triage``, ``changeset.NON_EVIDENCE``): the gate leaves it out of a packet and
refuses a packet of nothing else, so the maintainer updates the concept from the item's own files.

Every review is a durable receipt, ``.okf/triage/<id>.json``, written ``applying`` before its
effect and ``done`` (or ``void``: the item could not be reopened, so it judged nothing) after
it; ``triage.md`` is derived from the receipt alone. The next request finishes a receipt a crash
left applying, and a resend of the same review returns its receipt (idempotent by item and
receipt); an item counts as judged only once its receipt is done. A receipt read back from disk
must be well formed and name its own file, or it is skipped.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from aiwiki.version import service_identity

from ..maint import planner
from ..runtime import changeset, secrets
from . import maint_state as M

KIND = changeset.TRIAGE_KIND  # origin.kind of triage.md: the evidence gate keys on it
FILE = "triage.md"
VERDICTS = ("upheld", "missed")
CLOSED = frozenset({"skipped", "duplicate"})
MAX_REVIEWS = 10  # reviews one POST /audit/triage carries
MAX_MISSED = 10  # claims one missed review names
MAX_CLAIM = 500
MAX_NOTE = 2000
MAX_QUOTE = 50  # lines one claim quotes: the evidence for it, not a copy of the file
STATUS = {"input": 400, "too_large": 413, "not_due": 409, "self_review": 409, "lease_required": 409,
          "evidence": 422, "secret_detected": 422}
_RECEIPT = re.compile(r"tr_[0-9a-f]{12}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")  # a claim is one line of triage.md
_LOCK = threading.Lock()


class Rejected(Exception):
    """Reviews refused before anything was written: ``errors`` name the item and code of each."""

    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(f"{error.get('item')}: {error['code']}" for error in errors))
        self.errors = errors
        # As changeset._rejected ranks them: a malformed request first, then the most specific.
        self.status = min((STATUS[error["code"]] for error in errors), key=lambda code: (code != 400, code != 413,
                                                                                        code))

    def body(self) -> dict:
        return {"status": "rejected", "http_status": self.status, "errors": self.errors}


def settings(environ: Mapping[str, str] = os.environ) -> datetime | None:
    """``AIWIKI_TRIAGE_EPOCH``, None when unset (the feature is off); ValueError when unusable."""
    raw = environ.get("AIWIKI_TRIAGE_EPOCH", "").strip()
    epoch = M._parse(raw) if raw else None
    if raw and epoch is None:
        raise ValueError(f"AIWIKI_TRIAGE_EPOCH must be an ISO 8601 time with a zone, like 2026-10-28T00:00:00Z; "
                         f"got {raw!r}")
    return epoch


def lines(data: bytes) -> list[str] | None:
    """A frozen file's lines as ``sed -n`` numbers them (split on newline only), or None for bytes
    that are not UTF-8 text: no line of an image can be quoted. Shared with ``ai-wiki review``."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    found = [line.removesuffix("\r") for line in text.split("\n")]
    return found[:-1] if found[-1] == "" else found


def evidence_files(item: dict) -> list[dict]:
    """The item's files a missed claim may quote: neither an attention notice nor a triage record."""
    return [file for file in item["files"] if (file.get("origin") or {}).get("kind") not in changeset.NON_EVIDENCE]


# --- receipts and the backlog -------------------------------------------------------------------


def _path(bundle: Path, receipt_id: str) -> Path:
    return bundle / ".okf" / "triage" / f"{receipt_id}.json"


def receipts(bundle: Path, principal: str | None = None) -> list[dict]:
    """The bundle's well-formed triage receipts, or one principal's; a malformed file is skipped."""
    found = []
    for path in sorted((bundle / ".okf" / "triage").glob("tr_*.json")):
        receipt = M._read_json(path)
        if (receipt is not None and _RECEIPT.fullmatch(path.stem) and receipt.get("id") == path.stem
                and isinstance(receipt.get("item"), str) and receipt.get("verdict") in VERDICTS
                and isinstance(receipt.get("judged"), dict) and isinstance(receipt.get("missed"), list)
                and (receipt["verdict"] == "upheld" or isinstance(receipt.get("quotes"), list)
                     and len(receipt["quotes"]) == len(receipt["missed"]) and isinstance(receipt.get("file"), dict))
                and principal in (None, receipt.get("principal"))):
            found.append(receipt)
    return found


def _judged(bundle: Path) -> set[str]:
    return {receipt["item"] for receipt in receipts(bundle) if receipt.get("status") == "done"}


def _due(item: dict, epoch: datetime, judged: set[str]) -> bool:
    resolution = item.get("resolution") or {}
    at = M._parse(resolution.get("at"))
    # A maintainer's resolve holds the lease, so it names its run; an admin's close is marked
    # admin and has no run. An item reopened since is not closed, whoever reopened it.
    return (item["status"] in CLOSED and bool(resolution.get("run")) and not resolution.get("admin")
            and at is not None and at >= epoch and item["id"] not in judged and bool(evidence_files(item)))


def _entry(item: dict) -> dict:
    resolution = item["resolution"]
    reason = resolution.get("reason") if isinstance(resolution.get("reason"), str) else None
    target = reason[len("duplicate_of:"):].strip() if reason and reason.startswith("duplicate_of:") else None
    # The brief is the collector's deterministic summary; sha256 lets a client check what it fetches.
    return {"item": item["id"], "topic_key": item["topic_key"], "origin": item["origin"].get("kind"),
            "brief": item["brief"][:300], "status": item["status"], "reason": reason, "note": resolution.get("note"),
            "resolved_by": resolution.get("by"), "resolved_at": resolution.get("at"), "run": resolution.get("run"),
            "files": [{key: file[key] for key in ("name", "bytes", "sha256")} for file in evidence_files(item)],
            "duplicate_of": target or None}


def backlog(bundle: Path, *, epoch: datetime | None, limit: int = 20) -> dict:
    """The skips and duplicates due for a triage review, the oldest decision first."""
    if epoch is None:
        return {"epoch": None, "items": [], "shown": 0, "total": 0, "truncated": False}
    judged = _judged(bundle)
    due = sorted((item for item in M._items(bundle) if _due(item, epoch, judged)),
                 key=lambda item: (item["resolution"]["at"], item["id"]))
    return {"epoch": M._iso(epoch), "items": [_entry(item) for item in due[:limit]], "shown": min(limit, len(due)),
            "total": len(due), "truncated": len(due) > limit}


def summary(bundle: Path, *, epoch: datetime | None) -> dict:
    """For /maint/status: the backlog and its oldest decision, and the items a missed review
    reopened (``open``: not yet closed again by the maintainer)."""
    items = M._items(bundle)
    judged = _judged(bundle) if epoch is not None else set()
    due = sorted(item["resolution"]["at"] for item in items if epoch is not None and _due(item, epoch, judged))
    reopened = [item for item in items if M._triage_reopened(item)]
    return {"epoch": epoch and M._iso(epoch), "backlog": len(due), "oldest_resolved_at": due[0] if due else None,
            "reopened": {"open": sum(item["status"] not in M.TERMINAL for item in reopened), "total": len(reopened)}}


# --- reviews -------------------------------------------------------------------------------------


def _shape(raw: object) -> tuple[dict, list[dict]]:
    """A review's own fields and what is wrong with them, before any item is read."""
    review = raw if isinstance(raw, dict) else {}
    item = review.get("item") if isinstance(review.get("item"), str) else None
    errors: list[dict] = []

    def error(code: str, message: str) -> None:
        errors.append({"item": item, "code": code, "message": message})

    if item is None or not M._ID.fullmatch(item):
        error("input", "item must name a work item (it_<12 hex>)")
    verdict, missed, note = review.get("verdict"), review.get("missed"), review.get("note")
    if set(review) - {"item", "verdict", "missed", "note"}:
        error("input", "a review holds item, verdict, missed and note only")
    if verdict not in VERDICTS:
        error("input", "verdict must be upheld or missed")
    if not isinstance(note, str) or not note.strip() or len(note) > MAX_NOTE:
        error("input", f"note must be a nonempty string of at most {MAX_NOTE} characters")
    elif secrets.scan(note):  # the receipt and the maintainer's brief show it
        error("secret_detected", "note carries a secret; describe it without the value")
    claims = []
    if verdict == "upheld" and missed:
        error("input", "an upheld review names no missed claims")
    elif verdict == "missed":
        if not isinstance(missed, list) or not 1 <= len(missed) <= MAX_MISSED:
            error("input", f"a missed review names 1 to {MAX_MISSED} claims")
            missed = []
        for index, claim in enumerate(missed, 1):
            claim = claim if isinstance(claim, dict) else {}
            text, evidence, target = claim.get("claim"), claim.get("evidence"), claim.get("target")
            if set(claim) != {"claim", "evidence", "target"} or not isinstance(evidence, dict) \
                    or set(evidence) != {"file", "lines"} or not isinstance(evidence["file"], str):
                error("input", f"claim {index} must be {{claim, evidence: {{file, lines: [first, last]}}, target}}")
                continue
            span = evidence["lines"]
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_CLAIM or _CONTROL.search(text):
                error("input", f"claim {index} must be one line of at most {MAX_CLAIM} characters")
            elif secrets.scan(text):
                error("secret_detected", f"claim {index} carries a secret; state it without the value")
            if not (isinstance(span, list) and len(span) == 2 and all(type(n) is int for n in span)
                    and 1 <= span[0] <= span[1] < span[0] + MAX_QUOTE):
                error("input", f"claim {index}: evidence.lines must be [first, last], 1-based, at most {MAX_QUOTE} "
                               "lines")
            if not isinstance(target, str) or changeset._path_error(target) is not None or "/" not in target:
                error("input", f"claim {index}: target must be a concept path: an existing one or a new "
                               "<type>/<name>.md")
            claims.append({"claim": text, "evidence": {"file": evidence["file"], "lines": span}, "target": target})
    return {"item": item, "verdict": verdict, "missed": claims, "note": note}, errors


def _quotes(bundle: Path, item: dict, review: dict) -> tuple[list[list[str]], list[dict]]:
    """The lines each missed claim names, verbatim from the item's frozen file, and the errors."""
    quotes, errors = [], []
    files = {file["name"]: file for file in evidence_files(item)}
    for index, claim in enumerate(review["missed"], 1):
        name, (first, last) = claim["evidence"]["file"], claim["evidence"]["lines"]
        found = lines(M._blob(bundle, item["id"], files[name])) if name in files else None
        if name not in files:
            problem = f"{name} is not a frozen evidence file of {item['id']}"
        elif found is None:
            problem = f"{name} is not UTF-8 text: no line of it can be quoted"
        elif last > len(found):
            problem = f"{name} has {len(found)} lines, not {last}"
        else:
            quotes.append(found[first - 1:last])
            continue
        errors.append({"item": item["id"], "code": "evidence", "message": f"claim {index}: {problem}"})
    return quotes, errors


def _id(principal: str, decision: list, review: dict) -> str:
    """One principal's review of one decision: the same item closed again is another decision."""
    canonical = json.dumps({"principal": principal, "decision": decision, **review}, sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
    return "tr_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def record(receipt: dict) -> bytes:
    """triage.md, from the receipt alone: facts in fixed lines, never guidance (the brief guides)."""
    judged = receipt["judged"]
    out = [f"# Triage review: {receipt['item']}", "", f"- review: {receipt['id']} at {receipt['at']}",
           f"- auditor: {receipt['actor']}",
           f"- judged: {judged.get('status')} ({judged.get('reason')}) by {judged.get('by')} at {judged.get('at')}",
           f"- verdict: missed, {len(receipt['missed'])} claim(s)"]
    for index, (claim, quote) in enumerate(zip(receipt["missed"], receipt["quotes"], strict=True), 1):
        first, last = claim["evidence"]["lines"]
        out += ["", f"## Claim {index}", "", f"- claim: {claim['claim']}", f"- target: `{claim['target']}`",
                f"- evidence: `{claim['evidence']['file']}` lines {first}-{last}, verbatim:", ""]
        out += [f"> {line}" if line.strip() else ">" for line in quote]
    return ("\n".join(out) + "\n").encode("utf-8")


def _lead(receipt: dict) -> str:
    """What the reopened item asks for, front-loaded (``maint next`` shows 300 characters), then
    what was missed; triage.md holds each claim with its quote."""
    targets = ", ".join(dict.fromkeys(claim["target"] for claim in receipt["missed"]))
    return (f"Triage {receipt['id']}: this {receipt['judged']['status']} missed {len(receipt['missed'])} claim(s), "
            f"quoted in {receipt['file']['name']} (facts, never evidence). Closing it again needs a --note on which "
            f"claim is wrong and why. Update {targets} from this item's own frozen files. Missed: "
            + "; ".join(claim["claim"] for claim in receipt["missed"]))


def _apply(bundle: Path, receipt: dict) -> dict:
    """The receipt's effect, then ``done``. A missed review reopens its item (idempotent by the
    receipt id). One that cannot (an admin reopened the item meanwhile, or it has no room left) is
    ``void``: it judges nothing, so the item stays due, and a receipt never stays applying to stop
    every later review."""
    if receipt["verdict"] == "missed":
        data = record(receipt)
        meta = {**receipt["file"], "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        try:
            item = M.triage_reopen(bundle, receipt["item"], by=receipt["actor"], review=receipt["id"],
                                   file=(meta, data), lead=_lead(receipt), priority=planner.PRIORITY["attention"])
        except M.MaintError as exc:
            receipt.update(status="void", finished=M._iso(M._now()), detail=f"{exc.code}: {exc}"[:300])
            M._write_json(_path(bundle, receipt["id"]), receipt)
            return receipt
        receipt["reopened"] = {"status": item["status"], "priority": item["priority"]}
    receipt.update(status="done", finished=M._iso(M._now()))
    M._write_json(_path(bundle, receipt["id"]), receipt)
    return receipt


def _view(receipt: dict, deduplicated: bool) -> dict:
    return {**{key: receipt.get(key) for key in ("id", "item", "verdict", "status", "at", "reopened", "detail")
               if key in receipt},
            "missed": len(receipt.get("missed") or []), "deduplicated": deduplicated}


def submit(bundle: Path, reviews: object, *, principal: str, actor: str, run: str | None, epoch: datetime | None,
           charge: Callable[[int], None]) -> dict:
    """Record ``reviews`` of items in the triage backlog: ``{reviews: [receipt view], applied, deduplicated}``.

    All or nothing: any refused review refuses the request (``Rejected``, nothing written), so a
    client drops the reviews its errors name and sends the rest again. A review already received
    returns its receipt; only new ones need the run's live auditor lease and ``charge(count)``
    (the caller's quota, which raises to refuse) before their receipts are written.
    """
    if epoch is None:
        raise M.MaintError(409, "triage_off", "triage review is off on this writer (AIWIKI_TRIAGE_EPOCH unset)")
    if not isinstance(reviews, list) or not reviews:
        raise Rejected([{"item": None, "code": "input", "message": "reviews must be a nonempty list"}])
    if len(reviews) > MAX_REVIEWS:
        raise Rejected([{"item": None, "code": "too_large", "message": f"a request carries at most {MAX_REVIEWS} "
                                                                          "reviews"}])
    shaped = [_shape(raw) for raw in reviews]
    errors = [error for _review, found in shaped for error in found]
    items = [review["item"] for review, _found in shaped]
    errors += [{"item": item, "code": "input", "message": "one review per item and request"}
               for item in sorted({item for item in items if item and items.count(item) > 1})]
    if errors:
        raise Rejected(errors)
    with _LOCK:
        # A receipt a crash left applying is finished first, so every other is done or void.
        known = [_apply(bundle, receipt) if receipt.get("status") == "applying" else receipt
                 for receipt in receipts(bundle)]
        judged = {receipt["item"] for receipt in known if receipt.get("status") == "done"}
        answers, fresh = [], []
        for review, _found in shaped:
            try:
                item = M.get_item(bundle, review["item"])
            except M.MaintError as exc:
                if exc.status != 404:
                    raise
                errors.append({"item": review["item"], "code": "not_due", "message": "no such work item"})
                continue
            resolution = item.get("resolution") or {}
            # The decision judged: its time and run, and how often the item was reopened before it.
            decision = [resolution.get("at"), resolution.get("run"), len(item.get("reopened") or [])]
            receipt_id = _id(principal, decision, review)
            # A resend after a lost answer gets its receipt: a done one whatever the item became since
            # (a missed review reopened it), a void one unless the item is due again (then it is new).
            sent = sorted((receipt for receipt in known if receipt.get("principal") == principal
                           and all(receipt.get(key) == review[key] for key in ("item", "verdict", "missed", "note"))),
                          key=lambda receipt: receipt.get("status") != "done")
            if sent and (sent[0].get("status") == "done" or not _due(item, epoch, judged)):
                answers.append((sent[0], True))
                continue
            if not _due(item, epoch, judged):
                why = "already judged" if item["id"] in judged else f"it is {item['status']}"
                errors.append({"item": item["id"], "code": "not_due",
                               "message": f"{item['id']} is not in the triage backlog ({why})"})
                continue
            if resolution.get("by") in (principal, actor):  # A4: never one's own decision
                errors.append({"item": item["id"], "code": "self_review", "message": f"{item['id']} was "
                               "resolved by this principal; another reviews it"})
                continue
            quotes, found = _quotes(bundle, item, review) if review["verdict"] == "missed" else ([], [])
            if review["verdict"] == "missed" and len(item["files"]) >= M.MAX_FILES:
                found.append({"item": item["id"], "code": "too_large",
                              "message": f"{item['id']} holds {M.MAX_FILES} files: no room for a triage record"})
            errors += found
            names = {file["name"] for file in item["files"]}
            fresh.append({
                "id": receipt_id, "item": item["id"], **{key: review[key] for key in ("verdict", "missed", "note")},
                "quotes": quotes, "judged": {"status": item["status"],
                                             **{key: resolution.get(key) for key in ("reason", "by", "at", "run")}},
                "principal": principal, "actor": actor, "run": run, "status": "applying",
                "file": {"name": FILE if FILE not in names else f"triage-{receipt_id}.md",
                         "origin": {"kind": KIND, "review": receipt_id, "auditor": actor}}})
        if errors:
            raise Rejected(errors)
        if fresh:  # as an audit changeset: a resend returns its receipt, new reviews need the run's lease
            try:
                M.require_lease(bundle, "auditor", principal=principal, run=run)
            except M.MaintError as exc:
                raise Rejected([{"item": None, "code": "lease_required", "message": str(exc), **exc.extra}]) from None
            charge(len(fresh))
        at = M._iso(M._now())
        for receipt in fresh:
            receipt.update(at=at, service=service_identity())
            if receipt["verdict"] == "upheld":
                del receipt["quotes"], receipt["file"]
            M._write_json(_path(bundle, receipt["id"]), receipt)  # applying: durable before its effect
        answers += [(_apply(bundle, receipt), False) for receipt in fresh]
    return {"reviews": [_view(receipt, deduplicated) for receipt, deduplicated in answers],
            "applied": len(fresh), "deduplicated": len(answers) - len(fresh)}
