"""Receive side of the write path: land a submitted source in sources/inbox/ and track a job.

Deterministic, stdlib only. The actual curation is delegated to runtime/curate.py.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 5_000_000
_SLUG_RE = re.compile(r"[^\w一-鿿-]+")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(title: str | None, fallback: str) -> str:
    s = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-")
    return (s or fallback)[:60]


def write_source(bundle: Path, text: str, title: str | None = None) -> str:
    """Snapshot a submitted markdown source into sources/inbox/. Returns its bundle path."""
    if not text or not text.strip():
        raise ValueError("empty source text")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"source exceeds {MAX_BYTES} bytes")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    inbox = bundle / "sources" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / f"{slugify(title, 'ingest-' + sha[:8])}.md"
    n = 1
    while dest.exists():
        dest = inbox / f"{dest.stem.rsplit('-', 1)[0] if n > 1 else dest.stem}-{n}.md"
        n += 1
    fm = f"---\nsource_type: ingested\ningested: {_now()}\nsource_sha256: {sha}\n"
    if title:
        fm += f"title: {json.dumps(title, ensure_ascii=False)}\n"
    fm += "---\n\n"
    dest.write_text(fm + text.rstrip() + "\n", encoding="utf-8")
    return dest.relative_to(bundle).as_posix()


def job_path(bundle: Path, job_id: str) -> Path:
    return bundle / ".okf" / "jobs" / f"{job_id}.json"


def new_job(bundle: Path, source_rel: str) -> dict:
    (bundle / ".okf" / "jobs").mkdir(parents=True, exist_ok=True)
    job = {"id": uuid.uuid4().hex[:12], "source": source_rel, "status": "queued", "created": _now()}
    job_path(bundle, job["id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return job


def read_job(bundle: Path, job_id: str) -> dict | None:
    p = job_path(bundle, job_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
