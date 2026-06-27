"""FastAPI read API over an OKF bundle. Bearer-token authed. Read-only.

Config via env (read at import):
  AIWIKI_BUNDLE  path to the bundle (working clone)
  AIWIKI_TOKEN   bearer token clients must present
"""
from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from ..runtime import curate
from . import bundle as B
from . import ingest as I

_bundle = os.environ.get("AIWIKI_BUNDLE")
if not _bundle:
    raise RuntimeError("AIWIKI_BUNDLE is not set")
BUNDLE = Path(_bundle).expanduser().resolve()
TOKEN = os.environ.get("AIWIKI_TOKEN") or ""
if not TOKEN:
    raise RuntimeError("AIWIKI_TOKEN is not set")

# Endpoints listed (comma-separated) in AIWIKI_DISABLE return 403 — e.g. a "drill-only"
# deployment that forces progressive-disclosure navigation: AIWIKI_DISABLE=search,grep
DISABLED = {x.strip() for x in os.environ.get("AIWIKI_DISABLE", "").split(",") if x.strip()}

app = FastAPI(title="ai-wiki", version="0.0.1")


def _auth(authorization: str | None) -> None:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _enabled(name: str) -> None:
    if name in DISABLED:
        raise HTTPException(status_code=403, detail=f"endpoint '{name}' is disabled in this deployment")


@app.get("/health")
def health(authorization: str | None = Header(default=None)):
    _auth(authorization)
    types: Counter = Counter()
    statuses: Counter = Counter()
    total = 0
    for p, _rel in B.concepts(BUNDLE):
        fm, _ = B.parse(p.read_text(encoding="utf-8"))
        total += 1
        types[fm.get("type")] += 1
        statuses[fm.get("status")] += 1
    return {
        "bundle": BUNDLE.name,
        "concepts": total,
        "by_type": dict(types),
        "by_status": dict(statuses),
    }


@app.get("/ls")
def ls(dir: str | None = None, recursive: bool = False, show_all: bool = False,
       authorization: str | None = Header(default=None)):
    _auth(authorization)
    return {"items": B.list_dir(BUNDLE, dir, recursive=recursive, show_all=show_all)}


@app.get("/cat")
def cat(path: str = Query(...), authorization: str | None = Header(default=None)):
    _auth(authorization)
    try:
        p = B.safe_resolve(BUNDLE, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes bundle") from None
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"not found: {path}")
    return {"path": path, "content": p.read_text(encoding="utf-8")}


@app.get("/grep")
def grep(q: str = Query(...), dir: str | None = None, fixed: bool = False,
         authorization: str | None = Header(default=None)):
    _auth(authorization)
    _enabled("grep")
    try:
        return {"hits": B.grep(BUNDLE, q, dir, fixed=fixed)}
    except re.error as e:
        raise HTTPException(
            status_code=400,
            detail=f"invalid regex: {e}. Pass fixed=true for a literal search.",
        ) from None


@app.get("/search")
def search(q: str = Query(...), top_k: int = 10, authorization: str | None = Header(default=None)):
    _auth(authorization)
    _enabled("search")
    return {"results": B.search(BUNDLE, q, top_k)}


@app.get("/log")
def log(tail: int = 30, authorization: str | None = Header(default=None)):
    _auth(authorization)
    f = BUNDLE / "log.md"
    lines = f.read_text(encoding="utf-8").splitlines() if f.is_file() else []
    return {"lines": lines[-tail:]}


class IngestBody(BaseModel):
    text: str
    title: str | None = None


@app.post("/ingest")
def ingest(body: IngestBody, background: BackgroundTasks, authorization: str | None = Header(default=None)):
    """Land a submitted markdown source in sources/inbox/, then trigger a curation agent.

    Curation (a headless `claude -p` pass) runs in the background unless AIWIKI_CURATE=off.
    """
    _auth(authorization)
    _enabled("ingest")
    try:
        source_rel = I.write_source(BUNDLE, body.text, body.title)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    job = I.new_job(BUNDLE, source_rel)
    if os.environ.get("AIWIKI_CURATE", "auto") != "off":
        background.add_task(curate.run, BUNDLE, source_rel, I.job_path(BUNDLE, job["id"]))
        job["curation"] = "triggered"
    else:
        job["curation"] = "off"
    return job


@app.get("/jobs/{job_id}")
def get_job(job_id: str, authorization: str | None = Header(default=None)):
    _auth(authorization)
    job = I.read_job(BUNDLE, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
    return job
