"""FastAPI read API over OKF bundles. Bearer-token authed. Read-only reads.

One server can host *many* bundles (knowledge bases) under a single URL. Clients pick a
bundle per request with `?bundle=<name>`; `GET /bundles` lists them.

Config via env (read at import):
  AIWIKI_BUNDLES         root dir holding one bundle per subdirectory (multi-bundle mode)
  AIWIKI_BUNDLE          a single bundle dir (single-bundle mode; back-compat)
  AIWIKI_DEFAULT_BUNDLE  bundle used when a request omits ?bundle= (optional)
  AIWIKI_TOKEN           bearer token clients must present
  AIWIKI_DISABLE         comma-list of endpoints to 403 (ingest, audit, search, grep, create, delete)
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from aiwiki.version import VERSION

from ..runtime import audit as audit_runtime
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
        if not worker.recover(list(_registry().values())):
            raise RuntimeError(
                "curation recovery is awaiting remote commit confirmation; retry startup",
            )
        worker.ensure_started()
        worker.start_sweeper(lambda: list(_registry().values()))
    yield


app = FastAPI(title="ai-wiki", version=VERSION, lifespan=lifespan)


def _auth(authorization: str | None) -> None:
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _enabled(name: str) -> None:
    if name in DISABLED:
        raise HTTPException(status_code=403, detail=f"endpoint '{name}' is disabled in this deployment")


@contextmanager
def _read_window():
    """Keep the entire semantic filesystem read outside a writer mutation."""
    try:
        with worker.serialized_read():
            yield
    except worker.ReadBusy:
        raise HTTPException(
            status_code=503, detail="bundle mutation in progress; retry",
        ) from None


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
    with _read_window():
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
    created = False
    try:
        with worker.serialized_lifecycle():
            with worker.serialized_mutation(blocking=False):
                if target.parent != ROOT or target.exists():
                    raise HTTPException(status_code=409, detail=f"bundle '{name}' already exists")
                B.scaffold(target, name)
                created = True
                git = B.commit_scaffold(target, name)
    except worker.MutationBusy:
        raise HTTPException(status_code=409, detail="another bundle mutation is in progress; retry") from None
    except (OSError, RuntimeError) as exc:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"bundle creation failed: {exc}") from None
    return {"name": name, "created": True, "concepts": 0, "git": git}


@app.delete("/bundles/{name}")
def delete_bundle(name: str, authorization: str | None = Header(default=None)):
    """Delete a bundle and all its contents. Gated by AIWIKI_DISABLE=delete."""
    _auth(authorization)
    _enabled("delete")
    if SINGLE is not None:
        raise HTTPException(status_code=400, detail="server is in single-bundle mode (no AIWIKI_BUNDLES)")
    try:
        with worker.serialized_lifecycle():
            _name, p = _resolve(name)  # resolve while deletion is protected from ingest/create
            active = I.active_jobs(p)
            if active:
                raise HTTPException(
                    status_code=409,
                    detail=f"bundle has queued/running jobs: {', '.join(active[:5])}",
                )
            with worker.serialized_mutation(blocking=False):
                shutil.rmtree(p)
    except worker.MutationBusy:
        raise HTTPException(status_code=409, detail="another bundle mutation is in progress; retry") from None
    return {"name": _name, "deleted": True}


@app.get("/health")
def health(bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    with _read_window():
        name, BUNDLE = _resolve(bundle)
        return {"bundle": name, "service_version": VERSION, **B.health(BUNDLE)}


@app.get("/ls")
def ls(dir: str | None = None, recursive: bool = False, show_all: bool = False,
       bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    with _read_window():
        _name, BUNDLE = _resolve(bundle)
        try:
            return {"items": B.list_dir(BUNDLE, dir, recursive=recursive, show_all=show_all)}
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes bundle") from None


@app.get("/cat")
def cat(path: str = Query(...), bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    with _read_window():
        _name, BUNDLE = _resolve(bundle)
        try:
            p = B.safe_resolve(BUNDLE, path)
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes bundle") from None
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"not found: {path}")
        content = p.read_text(encoding="utf-8")
        result = {"path": path, "content": content}
        metadata = B.metadata_for_path(BUNDLE, p)
        if metadata is not None:
            result["metadata"] = metadata
        return result


@app.get("/grep")
def grep(q: str = Query(...), dir: str | None = None, fixed: bool = False,
         bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    _enabled("grep")
    with _read_window():
        _name, BUNDLE = _resolve(bundle)
        try:
            return {"hits": B.grep(BUNDLE, q, dir, fixed=fixed)}
        except re.error as e:
            raise HTTPException(
                status_code=400,
                detail=f"invalid regex: {e}. Pass fixed=true for a literal search.",
            ) from None


@app.get("/search")
def search(q: str = Query(...), top_k: int = Query(10, gt=0, le=1000), bundle: str | None = None,
           authorization: str | None = Header(default=None)):
    _auth(authorization)
    _enabled("search")
    with _read_window():
        _name, BUNDLE = _resolve(bundle)
        results = B.search(BUNDLE, q, None)
        return {"results": results[:top_k], "total": len(results)}


@app.get("/links")
def links(path: str = Query(...), bundle: str | None = None,
          authorization: str | None = Header(default=None)):
    _auth(authorization)
    with _read_window():
        _name, BUNDLE = _resolve(bundle)
        try:
            return B.links(BUNDLE, path)
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes bundle") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"not a concept: {path}") from None


@app.get("/log")
def log(tail: int = 30, bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    with _read_window():
        _name, BUNDLE = _resolve(bundle)
        f = BUNDLE / "log.md"
        lines = (
            f.read_text(encoding="utf-8").splitlines()
            if f.is_file() and not B.has_symlink_component(BUNDLE, f)
            else []
        )
        return {"lines": lines[-tail:]}


class IngestBody(BaseModel):
    text: str | None = None            # pasted text → stored as raw .md.source evidence
    content_b64: str | None = None     # any file (binary-safe), base64-encoded
    filename: str | None = None        # original name (drives the stored extension)
    title: str | None = None


@app.post("/ingest")
def ingest(body: IngestBody, bundle: str | None = None, authorization: str | None = Header(default=None)):
    """Land a submitted source (any type) in the bundle's sources/inbox/, then queue curation.

    Accepts pasted `text` (stored as raw .md.source evidence) or any file as
    `content_b64`+`filename` (stored
    verbatim). Sources claude can read (text/code/pdf/image) are queued for curation — a
    single serial worker processes one at a time, so concurrent ingests never race on the
    bundle/git. Other types are stored but flagged `needs-conversion`. Disabled with
    AIWIKI_CURATE=off; the whole endpoint is gated by AIWIKI_DISABLE=ingest.
    """
    _auth(authorization)
    _enabled("ingest")
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
        with worker.serialized_lifecycle():
            _name, BUNDLE = _resolve(bundle)  # re-resolve inside the delete exclusion window
            job, deduplicated = I.receive_source(BUNDLE, data, filename, body.title)
            if deduplicated:
                return {**job, "deduplicated": True}
            source_rel = job["source"]
            curatable = job["status"] == "queued"
            if curatable and CURATE_ON:
                job["curation"] = "queued"
            elif not curatable:
                job["curation"] = "needs-conversion"  # stored, not auto-curated
            else:
                job["curation"] = "off"
            I.save_job(BUNDLE, job)
            if curatable and CURATE_ON:
                worker.ensure_started()
                worker.submit(BUNDLE, source_rel, I.job_path(BUNDLE, job["id"]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {**job, "deduplicated": False}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, bundle: str | None = None, authorization: str | None = Header(default=None)):
    _auth(authorization)
    _name, BUNDLE = _resolve(bundle)
    job = I.read_job(BUNDLE, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
    return job


@app.post("/jobs/{ingest_job_id}/audit")
def audit(ingest_job_id: str, bundle: str | None = None,
          authorization: str | None = Header(default=None)):
    """Queue one idempotent adversarial review for a completed ingest job."""
    _auth(authorization)
    _enabled("audit")
    if not CURATE_ON:
        raise HTTPException(status_code=403, detail="audit requires AIWIKI_CURATE to be enabled")
    with worker.serialized_lifecycle():
        # The no-concept decision reads the live knowledge tree. Keep it in the
        # same read window as the durable parent check so an in-flight content pass
        # cannot turn a real audit into a false no-op success.
        with _read_window():
            _name, BUNDLE = _resolve(bundle)
            parent = I.read_job(BUNDLE, ingest_job_id)
            if parent is None:
                raise HTTPException(status_code=404, detail=f"no such ingest job: {ingest_job_id}")
            if parent.get("kind", "ingest") != "ingest":
                raise HTTPException(status_code=400, detail=f"job is not an ingest job: {ingest_job_id}")
            if parent.get("status") != "done":
                raise HTTPException(
                    status_code=409,
                    detail=f"ingest job must be done before audit (current: {parent.get('status')})",
                )
            if (parent.get("validation") or {}).get("status") != "passed":
                raise HTTPException(status_code=409, detail="ingest job did not pass deterministic validation")
            declared = parent.get("concept_files")
            concepts = audit_runtime.concept_files(BUNDLE, parent)
            if isinstance(declared, list) and declared and len(concepts) != len(set(declared)):
                missing = sorted(set(str(value) for value in declared) - set(concepts))
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "ingest audit scope is missing or invalid; retry after bundle repair: "
                        + ", ".join(missing[:10])
                    ),
                )
            job, deduplicated = I.receive_audit(BUNDLE, ingest_job_id, concepts)
        if deduplicated:
            return {**job, "deduplicated": True}
        if job["status"] == "queued":
            worker.ensure_started()
            worker.submit_audit(BUNDLE, ingest_job_id, I.job_path(BUNDLE, job["id"]))
    return {**job, "deduplicated": False}
