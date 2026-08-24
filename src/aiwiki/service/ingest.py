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
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 25_000_000
_SLUG_RE = re.compile(r"[^\w一-鿿.-]+")

# Sources Codex can curate directly: anything decodable as UTF-8 text (markdown, code,
# csv, json, html, …) plus image inputs. PDFs and other opaque binaries are stored but
# flagged needs-conversion rather than guessed (the writer has no PDF converter contract).
_READABLE_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_NEEDS_CONVERSION_EXT = {".pdf"}
_REUSABLE_JOB_STATUSES = {"queued", "running", "done", "needs-conversion"}
_REUSABLE_AUDIT_STATUSES = {"queued", "running", "done"}
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
    }
    if title:
        job["title"] = title
    if filename:
        job["original_name"] = filename
    job_path(bundle, job["id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return job


def find_job_by_sha(bundle: Path, sha: str) -> dict | None:
    """Return the newest reusable job for this exact source content."""
    jobs = bundle / ".okf" / "jobs"
    if not jobs.is_dir():
        return None
    for path in sorted(jobs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (job.get("kind", "ingest") == "ingest" and job.get("sha256") == sha
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


def find_audit_job(bundle: Path, parent_job: str) -> dict | None:
    """Return the newest reusable audit attempt for an ingest job.

    Queued/running attempts and terminal successful business results are
    idempotent. A technically failed attempt remains durable for diagnosis but
    must not permanently block a fresh retry.
    """
    jobs = bundle / ".okf" / "jobs"
    if not jobs.is_dir():
        return None
    for path in sorted(jobs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            job.get("kind") == "audit"
            and job.get("parent_job") == parent_job
            and job.get("status") in _REUSABLE_AUDIT_STATUSES
        ):
            return job
    return None


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


def save_job(bundle: Path, job: dict) -> None:
    job_path(bundle, job["id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def read_job(bundle: Path, job_id: str) -> dict | None:
    p = job_path(bundle, job_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


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
