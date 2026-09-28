"""Inbox intake (AIWIKI_INTAKE=inbox): a member submission becomes a maintainer work item.

POST /ingest and the inbox sweep land a source in ``sources/inbox/`` verbatim, as the Codex
path does, but for a bundle that commits changesets they queue a ``member`` work item instead
of Codex curation. The curating maintainer claims it through ``maint next`` like any collected
item, ahead of them by priority. The item holds the frozen evidence (text redacted of secrets as
collectors do, binaries verbatim) and names the submitter and the job; the job (``mode: inbox``)
names the item and answers ``GET /jobs/<id>`` from it, so the member follows one id to the
changeset and commit that curated it.

Every submission with content is also committed at once: an ``intake`` job commits the item's
frozen file (its redacted copy) to ``sources/inbox/intake/<slug>-<sha256><ext>`` and pushes it
in the writer transaction every service commit takes (strict pre-sync, commit, push, rollback;
``commit``), as ``intake: <title> (<principal>)`` with an ``Intake:`` trailer the audit backlog
reads as the service's own. The item records the path and commit (``intake``), and the job
answers with them. The copy sits apart from the Git-ignored drop zone the sweep scans, so the
sweep never takes it for a drop. A failed commit leaves the item ready; the sweep queues the job
again once its failure's retry time has passed. A link sent alone has no content to commit.

The writer never fetches a URL. A Feishu/Lark link sent without its content waits for the
maintainer, whose ``maint next`` reads it as the wiki's read-only Feishu app; any other link is
needs_access at once. A format nothing reads (a PDF) is needs_conversion. Text larger than one
evidence packet is refused, and an inbox drop of it is needs_conversion. ``requeue`` hands
unfinished member items back to Codex curation, the rollback while Codex exists.

Deterministic: nothing here runs an LLM, and only an intake commit opens a connection (Git's).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

from aiwiki.version import service_identity

from ..engine.document import has_symlink_component
from ..maint import planner
from ..runtime import changeset, curate, secrets
from ..runtime.failure import failure, phase_stage
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
INTAKE_DIR = "sources/inbox/intake"
_INTAKE_NAME = re.compile(r"[^./\\\x00-\x1f\x7f][^/\\\x00-\x1f\x7f]{0,254}")  # one plain path segment
# What one attempt of an intake job leaves on it; a retry starts without them.
_ATTEMPT = ("started", "finished", "phase", "base_revision", "base_branch", "pre_sync", "git", "commit", "error",
            "failure")


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
    """The item first, then its intake job, then the job naming both: a crash between them leaves
    an item the next submission (or sweep) finds again by its item_key and links to its own job,
    and at worst an intake job the sweep still commits."""
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
    # A link alone has no content to commit; an item already committed keeps its commit.
    intake = None if file["name"] == "link.txt" else (item.get("intake") or {}).get("job") or _new_intake(
        bundle, item["id"], job_id, file["name"], frozen, submitter=submitter, filename=filename, title=title,
        label=title or filename or url or "pasted text")
    job = {"id": job_id, "kind": "ingest", "mode": "inbox", "status": "inbox", "item": item["id"], "sha256": sha,
           "submitter": submitter, "via": via, "created": I._now(), "service": service_identity(),
           **{key: value for key, value in (("source", source), ("title", title), ("original_name", filename),
                                            ("url", url), ("intake", intake)) if value}}
    I._write_atomic(I.job_path(bundle, job_id), job)
    return job


def _new_intake(bundle: Path, item_id: str, job_id: str, name: str, frozen: bytes, *, submitter: str | None,
                filename: str | None, title: str | None, label: str) -> str:
    """Queue the commit of an item's frozen file, named in ``INTAKE_DIR`` like any inbox source."""
    # The name is committed: no secret in a file name or title reaches Git.
    filename, title = (secrets.redact(value)[0] if value else value for value in (filename, title))
    intake = {"id": uuid.uuid4().hex[:12], "kind": "intake", "status": "queued", "job": job_id, "item": item_id,
              "file": name, "sha256": hashlib.sha256(frozen).hexdigest(),
              "path": f"{INTAKE_DIR}/{I.source_name(frozen, filename, title)}", "title": label,
              "submitter": submitter, "created": I._now(), "service": service_identity()}
    I._write_atomic(I.job_path(bundle, intake["id"]), intake)
    return intake["id"]


def view(bundle: Path, job: dict) -> dict:
    """An inbox job as its member sees it: the state of its work item and of its intake commit,
    else the job as stored."""
    if job.get("mode") != "inbox":
        return job
    state = {**job, **_state(bundle, job.get("item"))}
    if job.get("intake"):
        state["intake"] = _intake_state(bundle, job["intake"])
    return state


def _intake_state(bundle: Path, intake_id: object) -> dict:
    record = None
    with contextlib.suppress(OSError, ValueError):
        record = I.read_job(bundle, intake_id) if isinstance(intake_id, str) and _JOB_ID.fullmatch(intake_id) else None
    if not isinstance(record, dict) or record.get("kind") != "intake":
        return {"job": intake_id, "status": "unknown"}
    status = record.get("status")
    state = {"job": record.get("id"), "status": "committed" if status == "done" else status, "path": record.get("path")}
    if status == "done":
        state["commit"] = record.get("commit")
    elif status == "failed":
        cause = record.get("failure") if isinstance(record.get("failure"), dict) else {}
        state["detail"] = (f"not in the wiki's Git yet: {cause.get('detail') or record.get('error')}; the writer "
                           + ("retries it" if cause.get("retryable") else "does not retry it, tell the owner"))
    else:
        state["detail"] = "not in the wiki's Git yet: being committed; ai-wiki jobs <id> shows its commit"
    return state


def commit(bundle: Path, job_path: Path) -> None:
    """Run one queued intake job: commit and push the redacted copy of a member submission.

    The serial worker holds the writer lock. It is the writer transaction of a revert without
    a gate: a clean tree, a strict pre-sync, the commit and its push (a rejected push rebases
    and retries, a lost acknowledgement counts as pushed), and a rollback on any failure. The
    bytes are the item's frozen file, re-hashed; a text copy still matching a secret rule is
    never committed. A copy Git already holds (another submission's, or a lost answer's) is
    done with the commit that holds it. Never raises.
    """
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(job, dict) or job.get("status") != "queued":
        return  # a duplicate queue entry of a job that already ran
    root = None
    try:
        root = curate._repo_root(bundle)
        _commit(root, bundle, job, job_path)
    except Exception as exc:  # noqa: BLE001 — record any failure on the job, never crash the worker
        git_timeout = isinstance(exc, subprocess.TimeoutExpired)
        job.update(status="failed", error=repr(exc), failure=failure(
            "transient" if git_timeout else "internal", stage=phase_stage(job), detail=repr(exc)))
        if root is not None and job.get("base_revision"):
            curate._rollback_git(root, job["base_revision"])
            job["phase"] = "rolled_back"
    job["finished"] = I._now()
    curate._save(job_path, job)


def _commit(root: Path | None, bundle: Path, job: dict, job_path: Path) -> None:
    if root is None or root.resolve() != bundle.resolve():
        return _failed(job, "internal", "intake", "an intake commit needs a bundle that is its own Git repository",
                       retryable=False)
    try:
        data = M.evidence(bundle, job["item"], job["file"], job["sha256"])[1]
    except M.MaintError as exc:
        return _failed(job, "internal", "intake", str(exc), retryable=False)
    try:
        found = secrets.scan(data.decode("utf-8"))
    except UnicodeDecodeError:
        found = []  # a binary is committed as submitted, as a changeset commits its packet
    target = bundle / job["path"]
    if found:
        return _failed(job, "input", "intake", f"the redacted copy still matches secret rule {found[0][0]}; "
                                               "nothing is committed")
    folder, _slash, name = job["path"].rpartition("/")  # .okf is not trusted: one plain file in INTAKE_DIR
    if folder != INTAKE_DIR or not _INTAKE_NAME.fullmatch(name) or name.endswith(".md") \
            or has_symlink_component(bundle, target):
        return _failed(job, "input", "intake", f"{job['path']} is not a plain file path under {INTAKE_DIR}")
    curate._exclude_inbox(root, bundle)
    if curate._working_files(root):
        return _failed(job, "internal", "git", "an intake commit needs a clean working tree")
    job.update(status="running", started=I._now(), service=service_identity(), phase="syncing",
               base_revision=curate._git(root, "rev-parse", "HEAD").stdout.strip(), base_branch=curate._branch(root))
    curate._save(job_path, job)
    job["pre_sync"] = curate._pre_sync(root, strict=True)  # never commit on a stale base
    if job["pre_sync"].get("refused"):
        return _failed(job, "transient", "pre_sync", f"pre-sync refused a stale base: {job['pre_sync']['note']}")
    job.update(base_revision=curate._git(root, "rev-parse", "HEAD").stdout.strip(), phase="prepared")
    curate._save(job_path, job)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    added = curate._git(root, "add", "-f", "--", job["path"])  # sources/inbox is Git-ignored
    if added.returncode != 0:
        raise RuntimeError(f"git add failed: {added.stderr.strip()[-200:]}")
    if curate._git(root, "diff", "--cached", "--quiet").returncode == 0:
        held = curate._git(root, "log", "-1", "--format=%H", "--", job["path"]).stdout.strip()
        job.update(status="done", phase="done", commit=held or None,
                   git={"committed": False, "pushed": False, "changed_files": [], "note": "already committed"})
        return settle(bundle, job)
    job["phase"] = "before_commit"
    curate._save(job_path, job)

    def persist(phase: str, result: dict) -> None:
        job.update(phase=phase, git=result, commit=result.get("commit"))
        curate._save(job_path, job)

    result = curate._commit_and_push(root, _message(job), 4, persist, bundle)
    job.update(git=result, commit=result.get("commit"))
    if result.get("committed") and (result.get("pushed") or not curate._has_remote(root)):
        job.update(status="done", phase="done")
        return settle(bundle, job)
    curate._rollback_git(root, job["base_revision"])
    job["phase"] = "rolled_back"
    note = str(result.get("note") or "").removesuffix(" (commit kept)")  # not here: it was rolled back
    _failed(job, "transient", "git", f"intake git commit/push failed: {note}")


def _failed(job: dict, cls: str, stage: str, detail: str, retryable: bool | None = None) -> None:
    job.update(status="failed", error=detail, failure=failure(cls, stage=stage, detail=detail, retryable=retryable))


def _message(job: dict) -> str:
    """``intake: <title> (<principal>)``, then the trailers of a service commit."""
    title = curate._one_line(job.get("title")) or "untitled"
    return (f"intake: {title} ({curate._one_line(job.get('submitter')) or 'service'})\n\n"
            f"Intake: {job['id']}\nWork-Items: {job['item']}\n")


def settle(bundle: Path, job: dict) -> None:
    """Record a done intake job's commit on its item; recovery repeats it after a crash."""
    try:
        M.record_intake(bundle, job["item"], job=job["id"], path=job["path"], commit=job.get("commit"))
    except M.MaintError as exc:  # the commit stands; the job still answers with it
        job["item_error"] = exc.detail()


def pending_intakes(bundle: Path) -> list[Path]:
    """The intake jobs to hand the worker: queued ones, and failed ones whose retry is due,
    queued again. Nothing is lost: a failure the writer cannot retry stays for the watchdog."""
    now, pending = M._now(), []
    for path in sorted((bundle / ".okf" / "jobs").glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(job, dict) or job.get("kind") != "intake":
            continue
        cause = job.get("failure") if isinstance(job.get("failure"), dict) else {}
        finished = M._parse(job.get("finished"))
        if job.get("status") == "failed" and cause.get("retryable") and finished is not None \
                and now >= finished + timedelta(seconds=cause.get("retry_after_s") or 0):
            job = {key: value for key, value in job.items() if key not in _ATTEMPT}
            job.update(status="queued", attempt=int(job.get("attempt") or 1) + 1)
            I._write_atomic(path, job)
        if job.get("status") == "queued":
            pending.append(path)
    return pending


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
