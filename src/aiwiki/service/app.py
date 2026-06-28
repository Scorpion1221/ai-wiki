"""FastAPI read API over OKF bundles. Bearer-token authed. Read-only reads.

One server can host *many* bundles (knowledge bases) under a single URL. Clients pick a
bundle per request with `?bundle=<name>`; `GET /bundles` lists them.

Config via env (read at import):
  AIWIKI_BUNDLES         root dir holding one bundle per subdirectory (multi-bundle mode)
  AIWIKI_BUNDLE          a single bundle dir (single-bundle mode; back-compat)
  AIWIKI_DEFAULT_BUNDLE  bundle used when a request omits ?bundle= (optional)
  AIWIKI_TOKEN           bearer token clients must present
  AIWIKI_DISABLE         comma-list of endpoints to 403 (ingest, search, grep, create, delete)
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from . import bundle as B
from . import ingest as I
from . import worker

# --- bundle root ---------------------------------------------------------------------
# Multi-bundle: AIWIKI_BUNDLES points at a dir of bundles. Single-bundle (back-compat):
# AIWIKI_BUNDLE points at one bundle, served under its own directory name.
_root = os.environ.get("AIWIKI_BUNDLES")
_single = os.environ.get("AIWIKI_BUNDLE")
if _root:
    ROOT: Path = Path(_root).expanduser().resolve()
    SINGLE: Path | None = None
elif _single:
    SINGLE = Path(_single).expanduser().resolve()
    ROOT = SINGLE.parent
else:
    raise RuntimeError("set AIWIKI_BUNDLES (multi-bundle root) or AIWIKI_BUNDLE (single bundle)")

DEFAULT = os.environ.get("AIWIKI_DEFAULT_BUNDLE") or None

TOKEN = os.environ.get("AIWIKI_TOKEN") or ""
if not TOKEN:
    raise RuntimeError("AIWIKI_TOKEN is not set")

# Endpoints listed (comma-separated) in AIWIKI_DISABLE return 403 — e.g. a read-only
# deploy uses AIWIKI_DISABLE=ingest,create,delete; a "drill-only" one adds search,grep.
DISABLED = {x.strip() for x in os.environ.get("AIWIKI_DISABLE", "").split(",") if x.strip()}
CURATE_ON = os.environ.get("AIWIKI_CURATE", "auto") != "off"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On a writer (curation enabled): start the serial worker, recover prior jobs, and
    # sweep the inbox on a timer to pick up sources dropped out-of-band.
    if CURATE_ON:
        worker.ensure_started()
        worker.recover(list(_registry().values()))
        worker.start_sweeper(lambda: list(_registry().values()))
    yield


app = FastAPI(title="ai-wiki", version="0.0.1", lifespan=lifespan)


def _auth(authorization: str | None) -> None:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _enabled(name: str) -> None:
    if name in DISABLED:
        raise HTTPException(status_code=403, detail=f"endpoint '{name}' is disabled in this deployment")


def _registry() -> dict[str, Path]:
    """Live map of bundle-name -> path (recomputed per call so newly added bundles appear)."""
    return {SINGLE.name: SINGLE} if SINGLE is not None else B.discover(ROOT)


def _resolve(name: str | None) -> tuple[str, Path]:
    """Pick the bundle for a request: explicit name, else the default, else the only one."""
    reg = _registry()
    if not reg:
        raise HTTPException(status_code=503, detail="no bundles available on this server")
    if name is None:
        if DEFAULT and DEFAULT in reg:
            name = DEFAULT
        elif len(reg) == 1:
            name = next(iter(reg))
        else:
            raise HTTPException(status_code=400,
                                detail="no bundle selected — pass ?bundle=<name>; see GET /bundles")
    p = reg.get(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no such bundle '{name}'; see GET /bundles")
    return name, p


def _default_name(reg: dict[str, Path]) -> str | None:
    if DEFAULT and DEFAULT in reg:
        return DEFAULT
    return next(iter(reg)) if len(reg) == 1 else None


@app.get("/bundles")
def bundles(authorization: str | None = Header(default=None)):
    """List the bundles this server hosts (name + concept count) and which is the default."""
    _auth(authorization)
    reg = _registry()
    return {
        "bundles": [{"name": n, "concepts": B.count_concepts(p)} for n, p in reg.items()],
        "default": _default_name(reg),
    }


class BundleBody(BaseModel):
    name: str


@app.post("/bundles", status_code=201)
def create_bundle(body: BundleBody, authorization: str | None = Header(default=None)):
    """Create a new empty bundle on this server (scaffolds a minimal, valid bundle)."""
    _auth(authorization)
    _enabled("create")
    if SINGLE is not None:
        raise HTTPException(status_code=400, detail="server is in single-bundle mode (no AIWIKI_BUNDLES)")
    name = body.name.strip()
    if not B.NAME_RE.match(name):
        raise HTTPException(status_code=400,
                            detail="invalid bundle name (use a-z 0-9 . _ - , starting alphanumeric)")
    target = (ROOT / name).resolve()
    if target.parent != ROOT or target.exists():
        raise HTTPException(status_code=409, detail=f"bundle '{name}' already exists")
    B.scaffold(target, name)
    return {"name": name, "created": True, "concepts": 0}


@app.delete("/bundles/{name}")
def delete_bundle(name: str, authorization: str | None = Header(default=None)):
    """Delete a bundle and all its contents. Gated by AIWIKI_DISABLE=delete."""
    _auth(authorization)
    _enabled("delete")
    if SINGLE is not None:
        raise HTTPException(status_code=400, detail="server is in single-bundle mode (no AIWIKI_BUNDLES)")
    _name, p = _resolve(name)  # 404s on unknown
    shutil.rmtree(p)
    return {"name": _name, "deleted": True}


@app.get("/health")
def health(bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    name, BUNDLE = _resolve(bundle)
    types: Counter = Counter()
    statuses: Counter = Counter()
    total = 0
    for p, _rel in B.concepts(BUNDLE):
        fm, _ = B.parse(p.read_text(encoding="utf-8"))
        total += 1
        types[fm.get("type")] += 1
        statuses[fm.get("status")] += 1
    return {"bundle": name, "concepts": total, "by_type": dict(types), "by_status": dict(statuses)}


@app.get("/ls")
def ls(dir: str | None = None, recursive: bool = False, show_all: bool = False,
       bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    _name, BUNDLE = _resolve(bundle)
    return {"items": B.list_dir(BUNDLE, dir, recursive=recursive, show_all=show_all)}


@app.get("/cat")
def cat(path: str = Query(...), bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    _name, BUNDLE = _resolve(bundle)
    try:
        p = B.safe_resolve(BUNDLE, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes bundle") from None
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"not found: {path}")
    return {"path": path, "content": p.read_text(encoding="utf-8")}


@app.get("/grep")
def grep(q: str = Query(...), dir: str | None = None, fixed: bool = False,
         bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    _enabled("grep")
    _name, BUNDLE = _resolve(bundle)
    try:
        return {"hits": B.grep(BUNDLE, q, dir, fixed=fixed)}
    except re.error as e:
        raise HTTPException(
            status_code=400,
            detail=f"invalid regex: {e}. Pass fixed=true for a literal search.",
        ) from None


@app.get("/search")
def search(q: str = Query(...), top_k: int = 10, bundle: str | None = None,
           authorization: str | None = Header(default=None)):
    _auth(authorization)
    _enabled("search")
    _name, BUNDLE = _resolve(bundle)
    return {"results": B.search(BUNDLE, q, top_k)}


@app.get("/log")
def log(tail: int = 30, bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    _name, BUNDLE = _resolve(bundle)
    f = BUNDLE / "log.md"
    lines = f.read_text(encoding="utf-8").splitlines() if f.is_file() else []
    return {"lines": lines[-tail:]}


class IngestBody(BaseModel):
    text: str | None = None            # pasted text → stored as .md
    content_b64: str | None = None     # any file (binary-safe), base64-encoded
    filename: str | None = None        # original name (drives the stored extension)
    title: str | None = None


@app.post("/ingest")
def ingest(body: IngestBody, bundle: str | None = None, authorization: str | None = Header(default=None)):
    """Land a submitted source (any type) in the bundle's sources/inbox/, then queue curation.

    Accepts pasted `text` (stored .md) or any file as `content_b64`+`filename` (stored
    verbatim). Sources claude can read (text/code/pdf/image) are queued for curation — a
    single serial worker processes one at a time, so concurrent ingests never race on the
    bundle/git. Other types are stored but flagged `needs-conversion`. Disabled with
    AIWIKI_CURATE=off; the whole endpoint is gated by AIWIKI_DISABLE=ingest.
    """
    _auth(authorization)
    _enabled("ingest")
    _name, BUNDLE = _resolve(bundle)
    if body.content_b64 is not None:
        try:
            data = base64.b64decode(body.content_b64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(status_code=400, detail="content_b64 is not valid base64") from None
        filename = body.filename or "upload"
    elif body.text is not None:
        data, filename = body.text.encode("utf-8"), body.filename
    else:
        raise HTTPException(status_code=400, detail="provide `text` or `content_b64`")
    try:
        source_rel, sha = I.write_source(BUNDLE, data, filename, body.title)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    curatable = I.is_curatable(source_rel, data)
    job = I.new_job(BUNDLE, source_rel, sha, curatable, body.title, filename)
    if curatable and CURATE_ON:
        worker.ensure_started()
        worker.submit(BUNDLE, source_rel, I.job_path(BUNDLE, job["id"]))
        job["curation"] = "queued"
    elif not curatable:
        job["curation"] = "needs-conversion"  # stored as a snapshot, not auto-curated
    else:
        job["curation"] = "off"
    return job


@app.get("/jobs/{job_id}")
def get_job(job_id: str, bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    _name, BUNDLE = _resolve(bundle)
    job = I.read_job(BUNDLE, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
    return job
