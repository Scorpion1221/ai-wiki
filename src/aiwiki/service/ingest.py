"""Receive side of the write path: land a submitted source in sources/inbox/ and track a job.

Sources are stored **as-is** — original bytes and original extension (Markdown and pasted
text use a ``.md.source`` suffix so they cannot be mistaken for concept documents). We
never mutate the file; provenance (sha, title, original name, time) lives on the job
record, and content-drift on sources/.hashes.yaml
(written by scan_sources). The actual curation is delegated to runtime/curate.py.

Deterministic, stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from aiwiki.version import service_identity

MAX_BYTES = 25_000_000
_SLUG_RE = re.compile(r"[^\w一-鿿.-]+")

# Sources Codex can curate directly: anything decodable as UTF-8 text (markdown, code,
# csv, json, html, …) plus image inputs. PDFs and other opaque binaries are stored but
# flagged needs-conversion rather than guessed (the writer has no PDF converter contract).
_READABLE_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_NEEDS_CONVERSION_EXT = {".pdf"}
_REUSABLE_JOB_STATUSES = {"queued", "running", "done", "needs-conversion"}
_REUSABLE_AUDIT_STATUSES = {"queued", "running", "done"}
# A rejected or failed changeset frees its idempotency key, so a fixed resubmission runs (§2.7).
_REUSABLE_CHANGESET_STATUSES = {"queued", "running", "done"}
# A done audit whose reviewer omitted or garbled its verdict judged no evidence. It stays a
# durable receipt, but the parent may be re-audited until this many such attempts exist.
_VERDICT_FORMAT_REASONS = {"verdict_missing", "verdict_invalid"}
MAX_VERDICT_FORMAT_AUDITS = 2
_JOB_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(title: str | None, fallback: str) -> str:
    s = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-.")
    return (s or fallback)[:60]


def is_curatable(filename: str, data: bytes) -> bool:
    extension = Path(filename).suffix.lower()
    if extension in _NEEDS_CONVERSION_EXT:
        return False
    if extension in _READABLE_BINARY_EXT:
        return True
    try:
        data.decode("utf-8")  # text / code / markup / csv / json / …
        return True
    except UnicodeDecodeError:
        return False


def write_source(bundle: Path, data: bytes, filename: str | None = None,
                 title: str | None = None) -> tuple[str, str]:
    """Snapshot a submitted source (raw bytes) into sources/inbox/. Returns (bundle-path, sha256).

    The file is stored verbatim under its original extension. Markdown/pasted text use a
    ``.md.source`` suffix to keep source evidence outside OKF concept discovery.
    """
    if not data or not data.strip():
        raise ValueError("empty source")
    if len(data) > MAX_BYTES:
        raise ValueError(f"source exceeds {MAX_BYTES} bytes")
    sha = hashlib.sha256(data).hexdigest()
    inbox = bundle / "sources" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    # The stored name carries the content sha, so two concurrent uploads of *different*
    # content can never map to the same path (kills the same-filename TOCTOU race), while
    # an exact re-upload maps to the same path and just rewrites identical bytes (idempotent).
    if filename:
        ext = Path(filename).suffix.lower() or ".source"
        # Raw Markdown is source evidence, not an OKF concept. Keep the submitted bytes
        # verbatim but prevent generic ``**/*.md`` tooling from parsing it as a concept.
        if ext == ".md":
            ext = ".md.source"
        name = f"{slugify(Path(filename).stem, 'ingest')}-{sha}{ext}"
    else:  # pasted text, no filename → raw Markdown source (not a concept document)
        name = f"{slugify(title, 'ingest')}-{sha}.md.source"
    dest = inbox / name
    dest.write_bytes(data)
    return dest.relative_to(bundle).as_posix(), sha


def job_path(bundle: Path, job_id: str) -> Path:
    return bundle / ".okf" / "jobs" / f"{job_id}.json"


def new_job(bundle: Path, source_rel: str, sha: str, curatable: bool,
            title: str | None = None, filename: str | None = None) -> dict:
    (bundle / ".okf" / "jobs").mkdir(parents=True, exist_ok=True)
    job = {
        "id": uuid.uuid4().hex[:12], "kind": "ingest", "source": source_rel, "sha256": sha,
        "status": "queued" if curatable else "needs-conversion",
        "created": _now(),
        "service": service_identity(),
    }
    if title:
        job["title"] = title
    if filename:
        job["original_name"] = filename
    job_path(bundle, job["id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return job


def find_job_by_sha(bundle: Path, sha: str) -> dict | None:
    """Return the newest reusable job for this exact source content.

    A changeset job names its packet's sha too, but it is no receipt for an upload: it may
    be a noop that stored nothing, or be rejected later. So an upload only reuses ingests.
    """
    jobs = bundle / ".okf" / "jobs"
    if not jobs.is_dir():
        return None
    for path in sorted(jobs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (job.get("kind", "ingest") == "ingest" and job.get("mode") != "changeset" and job.get("sha256") == sha
                and job.get("status") in _REUSABLE_JOB_STATUSES):
            return job
    return None


def receive_source(bundle: Path, data: bytes, filename: str | None = None,
                   title: str | None = None) -> tuple[dict, bool]:
    """Store new source content and create its job atomically; failed jobs remain retryable."""
    sha = hashlib.sha256(data).hexdigest()
    with _JOB_LOCK:
        existing = find_job_by_sha(bundle, sha)
        if existing is not None:
            return existing, True
        source_rel, sha = write_source(bundle, data, filename, title)
        curatable = is_curatable(source_rel, data)
        return new_job(bundle, source_rel, sha, curatable, title, filename), False


def _verdict_format_failure(job: dict) -> bool:
    audit = job.get("audit") if isinstance(job.get("audit"), dict) else {}
    return job.get("status") == "done" and audit.get("reason") in _VERDICT_FORMAT_REASONS


def _reusable_audit(attempts: list[dict]) -> dict | None:
    """The attempt a new audit request reuses, given one parent's attempts newest first."""
    for job in attempts:
        if job.get("status") not in _REUSABLE_AUDIT_STATUSES:
            continue
        if (_verdict_format_failure(job)
                and sum(map(_verdict_format_failure, attempts)) < MAX_VERDICT_FORMAT_AUDITS):
            return None
        return job
    return None


def find_audit_job(bundle: Path, parent_job: str) -> dict | None:
    """Return the newest reusable audit attempt for an ingest job.

    Queued/running attempts and terminal successful business results are
    idempotent. A technically failed attempt remains durable for diagnosis but
    must not permanently block a fresh retry; neither does a bounded number of done
    attempts whose verdict was missing or invalid.
    """
    jobs = bundle / ".okf" / "jobs"
    if not jobs.is_dir():
        return None
    attempts = []
    for path in sorted(jobs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("kind") == "audit" and job.get("parent_job") == parent_job:
            attempts.append(job)
    return _reusable_audit(attempts)


def new_audit_job(bundle: Path, parent_job: str, concept_files: list[str]) -> dict:
    """Create the queued adversarial-review job for a completed ingest job."""
    (bundle / ".okf" / "jobs").mkdir(parents=True, exist_ok=True)
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": "audit",
        "parent_job": parent_job,
        "concept_files": concept_files,
        "status": "queued",
        "created": _now(),
        "service": service_identity(),
    }
    if not concept_files:
        job.update({
            "status": "done",
            "finished": _now(),
            "reason": "no_concepts_to_audit",
            "validation": {"status": "passed", "error_count": 0},
            "commit": None,
            "changed_files": [],
            "audit": {
                "status": "passed",
                "verified_concepts": [],
                "unverified_concepts": [],
                "corrected_concepts": [],
            },
        })
    job_path(bundle, job["id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return job


def receive_audit(bundle: Path, parent_job: str, concept_files: list[str]) -> tuple[dict, bool]:
    """Reuse an active/successful audit, or create a retry after technical failure."""
    with _JOB_LOCK:
        existing = find_audit_job(bundle, parent_job)
        if existing is not None:
            return existing, True
        return new_audit_job(bundle, parent_job, concept_files), False


def changeset_path(bundle: Path, job_id: str) -> Path:
    """The persisted request of a changeset job, re-queued after a restart (§2.10)."""
    return bundle / ".okf" / "changesets" / f"{job_id}.json"


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def changeset_jobs(bundle: Path, principal: str | None = None) -> list[dict]:
    """The bundle's changeset jobs, or only those one principal submitted."""
    jobs = []
    for path in (bundle / ".okf" / "jobs").glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and job.get("mode") == "changeset" and principal in (None, job.get("principal")):
            jobs.append(job)
    return jobs


def find_changeset_job(bundle: Path, principal: str, digest: str) -> dict | None:
    """The principal's queued, running or done changeset with this idempotency key (§2.7).

    One an admin reverted is not reused: its content is gone, so proposing it again is new work.
    """
    matches = [job for job in changeset_jobs(bundle, principal)
               if job.get("changeset_sha256") == digest and job.get("status") in _REUSABLE_CHANGESET_STATUSES]
    reverted = reverted_by(bundle) if matches else {}
    return next((job for job in matches if job.get("id") not in reverted), None)


def new_changeset_job(bundle: Path, record: dict, **fields) -> dict:
    """Persist a changeset request, then its queued job (§2.5 G4). The request is written
    first, so no queued job exists without the payload a restart re-queues."""
    job = {"id": uuid.uuid4().hex[:12], "kind": "ingest", "mode": "changeset", "status": "queued",
           "created": _now(), "service": service_identity(), **fields}
    _write_atomic(changeset_path(bundle, job["id"]), record)
    _write_atomic(job_path(bundle, job["id"]), job)
    return job


def new_revert_job(bundle: Path, **fields) -> dict:
    """A queued admin revert (design §8.5). The job holds its whole request, so a restart
    re-queues it from the job alone."""
    job = {"id": uuid.uuid4().hex[:12], "kind": "revert", "status": "queued", "created": _now(),
           "service": service_identity(), **fields}
    _write_atomic(job_path(bundle, job["id"]), job)
    return job


def revert_jobs(bundle: Path) -> list[dict]:
    jobs = []
    for path in (bundle / ".okf" / "jobs").glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and job.get("kind") == "revert":
            jobs.append(job)
    return jobs


def _reverted(jobs: list[dict]) -> dict[str, str]:
    return {changeset: job["id"] for job in jobs if job.get("kind") == "revert" and job.get("status") == "done"
            and isinstance(job.get("id"), str) for changeset in job.get("reverted") or [] if isinstance(changeset, str)}


def reverted_by(bundle: Path) -> dict[str, str]:
    """Changeset job id -> the done admin revert that reverted it."""
    return _reverted(revert_jobs(bundle))


def save_job(bundle: Path, job: dict) -> None:
    job_path(bundle, job["id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def read_job(bundle: Path, job_id: str) -> dict | None:
    p = job_path(bundle, job_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def pending_audits(bundle: Path, *, older_than_hours: float = 24, limit: int = 20) -> dict:
    """Discover orphaned successful ingests, excluding active or completed audits."""
    jobs = []
    for path in (bundle / ".okf" / "jobs").glob("*.json"):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    attempts: dict = {}
    for job in sorted(jobs, key=lambda j: (j.get("created", ""), j.get("id", "")), reverse=True):
        if job.get("kind") == "audit":
            attempts.setdefault(job.get("parent_job"), []).append(job)
    reviewed = {parent for parent, audits in attempts.items() if _reusable_audit(audits) is not None}
    reviewed |= set(_reverted(jobs))  # nothing of a reverted changeset is left to audit
    failed_audits = {}
    for job in sorted(jobs, key=lambda j: (j.get("created", ""), j.get("id", ""))):
        if job.get("kind") == "audit" and job.get("status") == "failed":
            failed_audits.setdefault(job.get("parent_job"), []).append(job)
    cutoff = datetime.now(UTC).timestamp() - older_than_hours * 3600
    pending = []
    unscoped = 0
    for job in jobs:
        if (job.get("kind", "ingest") != "ingest" or job.get("status") != "done"
                or job.get("validation", {}).get("status") != "passed" or job.get("id") in reviewed):
            continue
        try:
            finished = datetime.fromisoformat(job.get("finished", job.get("created", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if finished.tzinfo is not None and finished.timestamp() <= cutoff:
            if not isinstance(job.get("concept_files"), list):
                unscoped += 1  # legacy receipt: do not falsely declare a no-concept audit
                continue
            pending.append({**job, "failed_audit_attempts": failed_audits.get(job["id"], [])})
    pending.sort(key=lambda j: (j.get("finished", j.get("created", "")), j["id"]))
    return {"jobs": pending[:limit], "shown": min(limit, len(pending)),
            "total": len(pending), "truncated": len(pending) > limit, "unscoped": unscoped}


def active_jobs(bundle: Path) -> list[str]:
    """IDs of queued/running jobs that make bundle deletion unsafe."""
    jobs = bundle / ".okf" / "jobs"
    if not jobs.is_dir():
        return []
    active: list[str] = []
    for path in sorted(jobs.glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("status") in {"queued", "running"}:
            active.append(str(job.get("id") or path.stem))
    return active
