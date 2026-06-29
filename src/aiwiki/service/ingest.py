"""Receive side of the write path: land a submitted source in sources/inbox/ and track a job.

Sources are stored **as-is** — original bytes, original extension (a pasted text snippet
with no filename defaults to .md). We never mutate the file; provenance (sha, title,
original name, time) lives on the job record, and content-drift on sources/.hashes.yaml
(written by scan_sources). The actual curation is delegated to runtime/curate.py.

Deterministic, stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 25_000_000
_SLUG_RE = re.compile(r"[^\w一-鿿.-]+")

# Sources claude can curate directly: anything decodable as UTF-8 text (markdown, code,
# csv, json, html, …) plus PDFs and images (read natively). Anything else is stored but
# flagged needs-conversion rather than auto-curated.
_READABLE_BINARY_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(title: str | None, fallback: str) -> str:
    s = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-.")
    return (s or fallback)[:60]


def is_curatable(filename: str, data: bytes) -> bool:
    if Path(filename).suffix.lower() in _READABLE_BINARY_EXT:
        return True
    try:
        data.decode("utf-8")  # text / code / markup / csv / json / …
        return True
    except UnicodeDecodeError:
        return False


def write_source(bundle: Path, data: bytes, filename: str | None = None,
                 title: str | None = None) -> tuple[str, str]:
    """Snapshot a submitted source (raw bytes) into sources/inbox/. Returns (bundle-path, sha256).

    The file is stored verbatim under its original extension (or .md for pasted text).
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
    short = sha[:8]
    if filename:
        ext = Path(filename).suffix.lower() or ".md"
        name = f"{slugify(Path(filename).stem, 'ingest')}-{short}{ext}"
    else:  # pasted text, no filename → markdown
        name = f"{slugify(title, 'ingest')}-{short}.md"
    dest = inbox / name
    dest.write_bytes(data)
    return dest.relative_to(bundle).as_posix(), sha


def job_path(bundle: Path, job_id: str) -> Path:
    return bundle / ".okf" / "jobs" / f"{job_id}.json"


def new_job(bundle: Path, source_rel: str, sha: str, curatable: bool,
            title: str | None = None, filename: str | None = None) -> dict:
    (bundle / ".okf" / "jobs").mkdir(parents=True, exist_ok=True)
    job = {
        "id": uuid.uuid4().hex[:12], "source": source_rel, "sha256": sha,
        "status": "queued" if curatable else "needs-conversion",
        "created": _now(),
    }
    if title:
        job["title"] = title
    if filename:
        job["original_name"] = filename
    job_path(bundle, job["id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return job


def read_job(bundle: Path, job_id: str) -> dict | None:
    p = job_path(bundle, job_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
