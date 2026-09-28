"""Inbox intake (AIWIKI_INTAKE=inbox): a member submission becomes a maintainer work item.

POST /ingest and the inbox sweep land a source in ``sources/inbox/`` verbatim, as the Codex
path does, but for a bundle that commits changesets they queue a ``member`` work item instead
of Codex curation. The curating maintainer claims it through ``maint next`` like any collected
item, ahead of them by priority. The item holds the frozen evidence (text redacted of secrets as
collectors do, binaries verbatim) and names the submitter and the job; the job (``mode: inbox``)
names the item and answers ``GET /jobs/<id>`` from it, so the member follows one id to the
changeset and commit that curated it.

The writer never fetches a URL. A Feishu/Lark link sent without its content waits for the
maintainer, whose ``maint next`` reads it as the wiki's read-only Feishu app; any other link is
needs_access at once. A format nothing reads (a PDF) is needs_conversion. Text larger than one
evidence packet is refused, and an inbox drop of it is needs_conversion. ``requeue`` hands
unfinished member items back to Codex curation, the rollback while Codex exists.

Deterministic: nothing here runs an LLM or opens a connection.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path

from aiwiki.version import service_identity

from ..maint import planner
from ..runtime import changeset, secrets
from . import ingest as I
from . import maint_state as M

NEEDS_ACCESS = ("nothing reads this link for you: the writer never fetches URLs and the maintainer reads only "
                "Feishu/Lark docs; save or export it and ingest the file")
NEEDS_CONVERSION = "the writer reads no such format: convert it to text or Markdown and ingest that"
TOO_LARGE = ("{size} bytes of text is more than one evidence packet holds ({limit}): split it into parts and "
             "ingest each")
_UNFINISHED = ("ready", "parked", "in_progress", "needs_human", "requeued")
_JOB_ID = re.compile(r"[0-9a-f]{12}")
_DAY = timedelta(days=1)


def receive(bundle: Path, data: bytes | None, *, filename: str | None, title: str | None, url: str | None,
            fetched: dict | None, submitter: str, quota: int | None = None) -> tuple[dict, bool]:
    """Store a member's submission and queue its work item; ``(job, deduplicated)``.

    ``data`` None is a link alone. The same content (or link) again returns its first job;
    anything new counts against ``quota``, the submitter's submissions to this bundle a day.
    A Feishu/Lark link the maintainer could not read (needs_access, nothing frozen but the link)
    reopens when sent again, as its reason tells the member to do once the wiki's app can read it.
    """
    with I._JOB_LOCK:
        sha = hashlib.sha256(data if data is not None else str(url).encode()).hexdigest()
        existing = I.find_job_by_sha(bundle, sha)
        if existing is not None:
            item = M._read_item(bundle, existing["item"]) if (
                data is None and planner.lark_link(str(url)) and existing.get("mode") == "inbox"
                and isinstance(existing.get("item"), str)) else None
            if item is not None and item["status"] == "needs_access" and [
                    file.get("name") for file in item["files"]] == ["link.txt"]:
                with contextlib.suppress(M.MaintError):  # an admin reopened it meanwhile
                    M.admin_retry(bundle, item["id"], principal=submitter, reason="resubmitted")
                return view(bundle, existing), False
            return view(bundle, existing), True
        file, outcome, reason = _freeze(data, filename, url)
        if outcome == "too_large":
            raise M.MaintError(413, "too_large", reason)
        if quota is not None:
            _check_quota(bundle, submitter, quota)
        source = I.write_source(bundle, data, filename, title)[0] if data is not None else None
        job = _register(bundle, sha, file, outcome, reason, source=source, submitter=submitter, via="ingest",
                        title=title, filename=filename, url=url, fetched=fetched)
    return view(bundle, job), False


def register_drop(bundle: Path, source: str, data: bytes) -> dict:
    """A file dropped in ``sources/inbox/`` out of band (the sweep): its job and work item."""
    with I._JOB_LOCK:
        sha = hashlib.sha256(data).hexdigest()
        existing = I.find_job_by_sha(bundle, sha)
        if existing is not None:
            return existing
        filename = Path(source).name.removesuffix(".source")
        file, outcome, reason = _freeze(data, filename, None)
        return _register(bundle, sha, file, "needs_conversion" if outcome == "too_large" else outcome, reason,
                         source=source, submitter=None, via="drop", title=None, filename=filename, url=None,
                         fetched=None)


def _freeze(data: bytes | None, filename: str | None, url: str | None) -> tuple[dict, str | None, str | None]:
    """The item's one evidence file ``{name, data, redactions}``, and the outcome closing it at
    once with its reason (``too_large``: text no evidence packet holds)."""
    if data is None:
        file = {"name": "link.txt", "data": f"{url}\n".encode(), "redactions": 0}
        return (file, None, None) if planner.lark_link(str(url)) else (file, "needs_access", NEEDS_ACCESS)
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(filename or "pasted.md").suffix)[:16]
    file = {"name": f"source{suffix}", "data": data, "redactions": 0}
    outcome = reason = None
    if not I.is_curatable(file["name"], data):
        outcome, reason = "needs_conversion", NEEDS_CONVERSION
    with contextlib.suppress(UnicodeDecodeError):  # binaries stay verbatim; the gate reads no secrets in text
        text, file["redactions"] = secrets.redact(data.decode("utf-8"))
        file["data"] = text.encode()
        limit = changeset.limits()["packet_text_bytes"]
        if outcome is None and len(file["data"]) > limit:
            outcome, reason = "too_large", TOO_LARGE.format(size=len(file["data"]), limit=limit)
    return file, outcome, reason


def _check_quota(bundle: Path, submitter: str, limit: int) -> None:
    """429 once ``submitter`` queued ``limit`` submissions into this bundle within a day."""
    now = M._now()
    times = []
    for path in (bundle / ".okf" / "jobs").glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        created = M._parse(job.get("created")) if isinstance(job, dict) else None
        if created and now - created < _DAY and job.get("via") == "ingest" and job.get("submitter") == submitter:
            times.append(created)
    if len(times) >= limit:
        retry = max(1, int(((sorted(times)[-limit] if limit else now) + _DAY - now).total_seconds()))
        raise M.MaintError(429, "rate_limited", f"the submissions_per_day quota of {limit} is used up for this "
                                                f"bundle; retry in {retry} s", retry_after_s=retry)


def _register(bundle: Path, sha: str, file: dict, outcome: str | None, reason: str | None, *, source: str | None,
              submitter: str | None, via: str, title: str | None, filename: str | None, url: str | None,
              fetched: dict | None) -> dict:
    """The item first, then the job naming it: a crash between them leaves an item the next
    submission (or sweep) finds again by its item_key and links to its own job."""
    job_id = uuid.uuid4().hex[:12]
    declared = {"title": title, "filename": filename, "url": url, "fetched": fetched, "source": source}
    origin = {"kind": M.MEMBER, "submitter": submitter, "via": via, "job": job_id, "sha256": sha,
              **{key: value for key, value in declared.items() if value}}
    if file["name"] != "link.txt":
        origin["redactions"] = file["redactions"]
    frozen = file["data"]
    [planned] = planner.plan([{
        "collector": "inbox", "topic_key": f"{M.MEMBER}:{sha[:16]}", "origin": origin,
        "brief": f"{submitter or 'inbox drop'}: {title or filename or url or 'pasted text'}",
        "files": [{"name": file["name"], "sha256": hashlib.sha256(frozen).hexdigest(), "bytes": len(frozen),
                   "data": frozen, "origin": {**origin, "kind": "member-source"}}]}])
    item = M.intake(bundle, planned, principal=submitter or "service", outcome=outcome, reason=reason)
    job = {"id": job_id, "kind": "ingest", "mode": "inbox", "status": "inbox", "item": item["id"], "sha256": sha,
           "submitter": submitter, "via": via, "created": I._now(), "service": service_identity(),
           **{key: value for key, value in (("source", source), ("title", title), ("original_name", filename),
                                            ("url", url)) if value}}
    I._write_atomic(I.job_path(bundle, job_id), job)
    return job


def view(bundle: Path, job: dict) -> dict:
    """An inbox job as its member sees it: the state of its work item, else the job as stored."""
    return {**job, **_state(bundle, job.get("item"))} if job.get("mode") == "inbox" else job


def _state(bundle: Path, item_id: object, depth: int = 0) -> dict:
    try:
        item = M.get_item(bundle, str(item_id))
    except M.MaintError as exc:
        return {"status": "unknown", "item_error": exc.detail()}
    resolution = item.get("resolution") or {}
    state = {"item": item["id"], "status": item["status"]}
    if resolution.get("reason"):
        state["reason"] = resolution["reason"]
    if item["status"] == "curated":
        state.update(changeset=resolution.get("job"), commit=resolution.get("commit"))
    if item["status"] == "split" and depth < 3:
        state["children"] = [_state(bundle, child, depth + 1) for child in resolution.get("children") or []]
    return state


def requeue(bundle: Path, *, principal: str, reason: str | None,
            only: list[str] | None = None) -> tuple[dict, list[tuple[str, Path]]]:
    """Hand unfinished member items (or ``only`` these) back to the Codex path, the inbox rollback.

    Each becomes requeued and its job an ordinary queued Codex ingest of its verbatim inbox
    source; the caller submits the returned ``(source, job path)`` pairs to the worker. An item
    the live maintainer run holds stays with it (``held``); one Codex cannot take, or no longer
    unfinished, is ``unavailable`` with the reason.
    """
    items = {item["id"]: item for item in M.list_items(bundle, origin=M.MEMBER, limit=10 ** 9)["items"]}
    ids = list(dict.fromkeys(only)) if only is not None else [
        item_id for item_id, item in items.items() if item["status"] in _UNFINISHED]
    intact, unavailable = [], []
    for item_id in ids:
        item = items.get(item_id)
        job = _job(bundle, item) if item else None
        if item is not None and item["status"] == "requeued" and job and job.get("mode") != "inbox":
            if only is not None:  # handed over before; by default a finished requeue is no news
                unavailable.append({"item": item_id, "error": f"it went to Codex already as job {job['id']}"})
            continue
        if item is None:
            error = "no such member item"
        elif job is None:
            error = "its job record is missing or invalid"
        elif not job.get("source"):
            error = "a link with no stored source: Codex cannot take it; close it with POST /admin/items/<id>/resolve"
        elif not _intact(bundle, job):
            error = "its verbatim source is missing from sources/inbox"
        else:
            intact.append(item_id)
            continue
        unavailable.append({"item": item_id, "error": error})
    closed = M.requeue(bundle, intact, principal=principal, reason=reason)
    unavailable += closed["refused"]
    queued, requeued = [], []
    for item in closed["requeued"]:
        job = _job(bundle, item)
        if (job or {}).get("mode") != "inbox":
            continue
        path = I.job_path(bundle, job["id"])
        job.pop("mode")
        job.update(status="queued", curation="queued",
                   requeued={"item": item["id"], "by": principal, "at": I._now()})
        I._write_atomic(path, job)
        queued.append((job["source"], path))
        requeued.append({"item": item["id"], "job": job["id"]})
    return {"requeued": requeued, "held": closed["held"], "unavailable": unavailable}, queued


def _intact(bundle: Path, job: dict) -> bool:
    """The job's verbatim source is in sources/inbox with its sha. ``.okf`` is written in place
    by the Codex audit, so a job read back must name an inbox file."""
    rel = job["source"]
    if not (isinstance(rel, str) and rel.startswith("sources/inbox/") and ".." not in rel.split("/")):
        return False
    source = bundle / rel
    with contextlib.suppress(OSError):
        return not source.is_symlink() and hashlib.sha256(source.read_bytes()).hexdigest() == job.get("sha256")
    return False


def _job(bundle: Path, item: dict) -> dict | None:
    """The job an item names, when it is a well-formed job of that id."""
    job_id = item["origin"].get("job")
    if not (isinstance(job_id, str) and _JOB_ID.fullmatch(job_id)):
        return None
    try:
        job = I.read_job(bundle, job_id)
    except (OSError, ValueError):
        return None
    return job if isinstance(job, dict) and job.get("id") == job_id else None
