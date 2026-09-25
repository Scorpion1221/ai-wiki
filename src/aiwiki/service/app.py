"""FastAPI read API over OKF bundles. Bearer-token authed, scoped per principal.

One server can host *many* bundles (knowledge bases) under a single URL. Clients pick a
bundle per request with `?bundle=<name>`; `GET /bundles` lists them.

Config via env (read at import):
  AIWIKI_BUNDLES         root dir holding one bundle per subdirectory (multi-bundle mode)
  AIWIKI_BUNDLE          a single bundle dir (single-bundle mode; back-compat)
  AIWIKI_DEFAULT_BUNDLE  bundle used when a request omits ?bundle= (optional)
  AIWIKI_PRINCIPALS      principals file (hashed tokens + scopes, see auth.py); SIGHUP reloads it
  AIWIKI_TOKEN           legacy shared bearer token with every scope, used when no principals file
  AIWIKI_DISABLE         comma-list of endpoints to 403 (ingest, audit, search, grep, create, delete,
                         maint, admin, changesets, workspace)
  AIWIKI_INTAKE, AIWIKI_AUDIT, AIWIKI_CHANGESETS_COMMIT, AIWIKI_RESTRUCTURE, AIWIKI_CODEX_AUDIT_MANUAL
                         rollout switches reported by /whoami; unset keeps today's behaviour. Only
                         bundles listed in AIWIKI_CHANGESETS_COMMIT commit changesets (none while
                         AIWIKI_DISABLE=changesets); every bundle may dry-run. A committed changeset
                         queues its Codex audit unless its bundle is listed in
                         AIWIKI_CODEX_AUDIT_MANUAL (a shadow bundle), whose audits run on request only
  AIWIKI_CHANGESET_WAIT_S  seconds POST /changesets waits for its receipt before a 202 (default 60)
  AIWIKI_CHANGESETS_PER_HOUR, AIWIKI_CHANGESETS_PER_DAY, AIWIKI_DEPRECATIONS_PER_DAY
                         default changeset quotas of a principal without its own `limits`

The writer must receive /whoami, /workspace, /changesets, /maint, /audit/backlog and /admin
as well as /ingest and /jobs (the Cloudflare route regex of design §2.1); a read mirror
disables changesets, workspace, maint and admin.
"""
from __future__ import annotations

import base64
import binascii
import difflib
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from aiwiki.version import VERSION, build, service_identity

from ..runtime import audit as audit_runtime
from ..runtime import changeset, secrets
from ..runtime import curate as curate_runtime
from ..runtime.failure import failure
from . import auth, worker
from . import bundle as B
from . import ingest as I
from . import maint_state as M

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

# Refuses to start on an invalid principals file (e.g. a process holding curate and audit).
AUTH = auth.Registry.from_env()

# Endpoints listed (comma-separated) in AIWIKI_DISABLE return 403 — e.g. a read-only deploy uses
# AIWIKI_DISABLE=ingest,audit,create,delete,changesets,workspace,maint,admin; a "drill-only" one
# adds search,grep.
DISABLED = {x.strip() for x in os.environ.get("AIWIKI_DISABLE", "").split(",") if x.strip()}
CURATE_ON = os.environ.get("AIWIKI_CURATE", "auto") != "off"


def _mode(name: str, choices: tuple[str, ...]) -> str:
    value = os.environ.get(name, "").strip() or choices[0]
    if value not in choices:
        raise RuntimeError(f"{name} must be one of {', '.join(choices)}; got {value!r}")
    return value


def _bundles(name: str) -> list[str]:
    return sorted({x.strip() for x in os.environ.get(name, "").split(",") if x.strip()})


# Rollout switches; the defaults keep the pre-changeset behaviour. A value is accepted only
# once the service honours it, so a premature flip fails at startup and /whoami never
# misreports one: AIWIKI_AUDIT=external waits for the audit changeset gate (phase 4a) and
# AIWIKI_RESTRUCTURE=on for the restructure intent (phase 4b).
MODES = {
    "intake": _mode("AIWIKI_INTAKE", ("curate",)),  # /ingest has no inbox mode yet
    "audit": _mode("AIWIKI_AUDIT", ("codex",)),
    # The bundles that commit, as the worker applies it: none while the route is disabled.
    "changesets_commit": [] if "changesets" in DISABLED else _bundles("AIWIKI_CHANGESETS_COMMIT"),
    "restructure": _mode("AIWIKI_RESTRUCTURE", ("off",)),
    "codex_audit_manual": _bundles("AIWIKI_CODEX_AUDIT_MANUAL"),
}
API = {"changesets": 1}
CLIENT_MIN = "0.3.0"
# A changeset answers synchronously within this, well inside Cloudflare's ~100s origin timeout.
WAIT_S = float(os.environ.get("AIWIKI_CHANGESET_WAIT_S", "60"))
# Changeset quotas of a principal (design §2.2): its own `limits` override these defaults.
QUOTAS = {
    "changesets_per_hour": (timedelta(hours=1), "AIWIKI_CHANGESETS_PER_HOUR", 30),
    "changesets_per_day": (timedelta(days=1), "AIWIKI_CHANGESETS_PER_DAY", 150),
    "deprecations_per_day": (timedelta(days=1), "AIWIKI_DEPRECATIONS_PER_DAY", 10),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On a writer (curation enabled): start the serial worker, recover prior jobs, and
    # sweep the inbox on a timer to pick up sources dropped out-of-band.
    with auth.reload_on_sighup(AUTH):
        if CURATE_ON:
            if not worker.recover(list(_registry().values())):
                raise RuntimeError(
                    "curation recovery is awaiting remote commit confirmation; retry startup",
                )
            worker.ensure_started()
            worker.start_sweeper(lambda: list(_registry().values()))
        yield


app = FastAPI(title="ai-wiki", version=VERSION, lifespan=lifespan)


def _auth(authorization: str | None, *scopes: str) -> auth.Principal:
    """The caller's principal, holding any of `scopes` (none: any valid token) — else 401/403."""
    try:
        return AUTH.authorize(authorization, *scopes)
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None


def _allow(principal: auth.Principal, name: str) -> None:
    if not principal.allows(name):
        raise HTTPException(status_code=403, detail=f"principal {principal.id} may not access bundle '{name}'")


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


def _resolve(name: str | None, principal: auth.Principal) -> tuple[str, Path]:
    """Pick the bundle for a request: explicit name, else the default, else the only one."""
    reg = _registry()
    if not reg:
        raise HTTPException(status_code=503, detail="no bundles available on this server")
    if name is None:
        name = _default_name(reg)
        if name is None or not principal.allows(name):  # as GET /bundles, never name a hidden default
            raise HTTPException(status_code=400,
                                detail="no bundle selected — pass ?bundle=<name>; see GET /bundles")
    _allow(principal, name)  # before the lookup, so a disallowed name reveals nothing
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
    principal = _auth(authorization, "read")
    with _read_window():
        reg = _registry()
        default = _default_name(reg)
        return {
            "bundles": [{"name": n, "concepts": B.count_concepts(p)} for n, p in reg.items() if principal.allows(n)],
            "default": default if default and principal.allows(default) else None,
        }


class BundleBody(BaseModel):
    name: str


@app.post("/bundles", status_code=201)
def create_bundle(body: BundleBody, authorization: str | None = Header(default=None)):
    """Create a new empty bundle on this server (scaffolds a minimal, valid bundle)."""
    principal = _auth(authorization, "admin")
    _enabled("create")
    if SINGLE is not None:
        raise HTTPException(status_code=400, detail="server is in single-bundle mode (no AIWIKI_BUNDLES)")
    name = body.name.strip()
    _allow(principal, name)
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
    principal = _auth(authorization, "admin")
    _enabled("delete")
    if SINGLE is not None:
        raise HTTPException(status_code=400, detail="server is in single-bundle mode (no AIWIKI_BUNDLES)")
    try:
        with worker.serialized_lifecycle():
            _name, p = _resolve(name, principal)  # resolve while deletion is protected from ingest/create
            if (ROOT / _name).is_symlink():  # a linked bundle lives elsewhere (B.discover): not this server's to delete
                raise HTTPException(status_code=409, detail=f"bundle '{_name}' is linked into the bundles root; "
                                                            "remove the link on the host instead")
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


@app.get("/whoami")
def whoami(authorization: str | None = Header(default=None)):
    """Who this token is on the writer; clients check `api` and `client.min` for compatibility.

    ``writer`` is false on a read mirror (AIWIKI_CURATE=off), so a /whoami the mirror answers
    by mistake never passes for the writer's.
    """
    principal = _auth(authorization)
    body = {
        "writer": CURATE_ON,
        "principal": principal.id,
        "actor": principal.actor,
        "role": principal.role,
        "scopes": sorted(principal.scopes),
        "bundles": sorted(principal.bundles) if principal.bundles is not None else None,
        "limits": dict(principal.limits),
        "remaining_today": None,  # per-principal quotas are not metered yet
        "api": API,
        "client": {"min": CLIENT_MIN},
        "service": service_identity(),
        "modes": MODES,
    }
    if "admin" in principal.scopes:
        body["auth"] = AUTH.receipt()  # confirms a SIGHUP reload took; a refused one only logs
    return body


@app.get("/health")
def health(bundle: str | None = None, authorization: str | None = Header(default=None)):
    principal = _auth(authorization, "read")
    with _read_window():
        name, BUNDLE = _resolve(bundle, principal)
        result = {"bundle": name, "service_version": VERSION, "build": build(), **B.health(BUNDLE)}
        if CURATE_ON:
            result["writer_agent"] = curate_runtime._agent_metadata()
        return result


@app.get("/ls")
def ls(dir: str | None = None, recursive: bool = False, show_all: bool = False,
       bundle: str | None = None, authorization: str | None = Header(default=None)):
    principal = _auth(authorization, "read")
    with _read_window():
        _name, BUNDLE = _resolve(bundle, principal)
        try:
            return {"items": B.list_dir(BUNDLE, dir, recursive=recursive, show_all=show_all)}
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes bundle") from None


@app.get("/cat")
def cat(path: str = Query(...), bundle: str | None = None, authorization: str | None = Header(default=None)):
    principal = _auth(authorization, "read")
    with _read_window():
        _name, BUNDLE = _resolve(bundle, principal)
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
    principal = _auth(authorization, "read")
    _enabled("grep")
    with _read_window():
        _name, BUNDLE = _resolve(bundle, principal)
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
    principal = _auth(authorization, "read")
    _enabled("search")
    with _read_window():
        _name, BUNDLE = _resolve(bundle, principal)
        results = B.search(BUNDLE, q, None)
        return {"results": results[:top_k], "total": len(results)}


@app.get("/links")
def links(path: str = Query(...), bundle: str | None = None,
          authorization: str | None = Header(default=None)):
    principal = _auth(authorization, "read")
    with _read_window():
        _name, BUNDLE = _resolve(bundle, principal)
        try:
            return B.links(BUNDLE, path)
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes bundle") from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"not a concept: {path}") from None


@app.get("/log")
def log(tail: int = Query(30, ge=0), bundle: str | None = None,
        authorization: str | None = Header(default=None)):
    principal = _auth(authorization, "read")
    with _read_window():
        _name, BUNDLE = _resolve(bundle, principal)
        f = BUNDLE / "log.md"
        lines = (
            f.read_text(encoding="utf-8").splitlines()
            if f.is_file() and not B.has_symlink_component(BUNDLE, f)
            else []
        )
        # append_log prepends new dated sections, so the start of the file is the newest.
        return {"lines": lines[:tail] if tail else [], "total": len(lines)}


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
    verbatim). Sources Codex can read (text/code/image) are queued for curation — a
    single serial worker processes one at a time, so concurrent ingests never race on the
    bundle/git. Other types are stored but flagged `needs-conversion`. Disabled with
    AIWIKI_CURATE=off; the whole endpoint is gated by AIWIKI_DISABLE=ingest.
    """
    principal = _auth(authorization, "submit")
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
            _name, BUNDLE = _resolve(bundle, principal)  # re-resolve inside the delete exclusion window
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


@app.get("/jobs/pending-audit")
def list_pending_audits(
    bundle: str | None = None,
    older_than_hours: float = Query(default=24, ge=0, le=87600),
    limit: int = Query(default=20, ge=1, le=100),
    authorization: str | None = Header(default=None),
):
    """Read-only discovery of completed ingests still missing a terminal audit."""
    principal = _auth(authorization, "read")
    _name, BUNDLE = _resolve(bundle, principal)
    return I.pending_audits(BUNDLE, older_than_hours=older_than_hours, limit=limit)


@app.get("/jobs/{job_id}")
def get_job(job_id: str, bundle: str | None = None, authorization: str | None = Header(default=None)):
    principal = _auth(authorization, "read")
    _name, BUNDLE = _resolve(bundle, principal)
    job = I.read_job(BUNDLE, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
    return job


@app.post("/jobs/{ingest_job_id}/audit")
def audit(ingest_job_id: str, bundle: str | None = None,
          authorization: str | None = Header(default=None)):
    """Queue one idempotent adversarial review for a completed ingest job.

    The server-side reviewer does the verifying, so the curator may request it too: the
    maintenance loop re-submits Codex audits until the external auditor takes over
    (AIWIKI_AUDIT=external), after which this route answers 409.
    """
    principal = _auth(authorization, "audit", "curate")
    _enabled("audit")
    if MODES["audit"] == "external":
        raise HTTPException(status_code=409, detail="audit is external")
    if not CURATE_ON:
        raise HTTPException(status_code=403, detail="audit requires AIWIKI_CURATE to be enabled")
    with worker.serialized_lifecycle():
        _name, BUNDLE = _resolve(bundle, principal)
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
        try:
            # The no-concept decision reads the live knowledge tree. Keep it in a read
            # window so an in-flight content pass cannot turn a real audit into a false
            # no-op success.
            with worker.serialized_read():
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
        except worker.ReadBusy:
            # A long agent pass owns the live tree. The durable parent receipt already
            # scopes the audit, and the queued runtime re-checks that scope on the live
            # tree under its own serialized mutation, so enqueue instead of failing.
            if not isinstance(declared, list):
                raise HTTPException(
                    status_code=409,
                    detail="bundle mutation in progress; unscoped legacy ingest audit not created; retry",
                ) from None
            concepts = sorted({value for value in declared if isinstance(value, str)})
            job, deduplicated = I.receive_audit(BUNDLE, ingest_job_id, concepts)
        if deduplicated:
            return {**job, "deduplicated": True}
        if job["status"] == "queued":
            worker.ensure_started()
            worker.submit_audit(BUNDLE, ingest_job_id, I.job_path(BUNDLE, job["id"]))
    return {**job, "deduplicated": False}


# --- the published workspace and curate changesets (design §2.1–§2.9) ---------------------
# The gate itself (G6–G14) is runtime/changeset.py and curate.run_changeset; this section only
# admits a request (G0–G4), queues it on the serial worker and answers with its receipt.

_CHANGESET_SCOPES = {"curate": ("curate", "admin"), "audit": ("audit", "admin")}


def _actor_of(principal_id: str, bundle: str) -> str | None:
    """The actor a principal now in force (a SIGHUP may have removed it) stamps on a curate
    changeset to ``bundle``, or None when it may no longer propose one."""
    today = datetime.now(UTC).date()
    return next((p.actor for p in AUTH.principals
                 if p.id == principal_id and p.allows(bundle) and not p.scopes.isdisjoint(_CHANGESET_SCOPES["curate"])
                 and (p.expires is None or today <= p.expires)), None)


# The worker admits a queued changeset again as it runs (design §2.11 rollback, §8.5 revocation).
worker.COMMIT_BUNDLES = frozenset(MODES["changesets_commit"])
# Phase 2 (design §9): the shadow bundle's Codex audits wait for the admin cron's request, so
# they neither queue by themselves nor jump ahead of production's Codex work.
worker.AUDIT_BUNDLES = worker.COMMIT_BUNDLES - set(MODES["codex_audit_manual"])
worker.actor_of = _actor_of


class _Rejected(Exception):
    """A changeset refused before it has a job: the gate's rejection body and HTTP status."""

    def __init__(self, result: dict, headers: dict | None = None):
        super().__init__(result["http_status"])
        self.result, self.headers = result, headers or {}


_REVISION = re.compile(r"[0-9a-f]{7,64}")


def _published(bundle: Path, at: str | None = None) -> tuple[Path, str, bytes]:
    """The repository, the published revision and its bundle tree as a tar (design §2.1).

    That is the pushed commit (origin/<branch>, or HEAD without a remote) read from the
    object store, so no lock is needed and no transaction in progress is ever seen. ``at``
    names an earlier published revision instead (the base of ``admin compare``); anything
    that is not an ancestor of the published one is a 404.
    """
    root = curate_runtime._repo_root(bundle)
    if root is None:
        raise HTTPException(status_code=503, detail="the bundle has no Git repository to publish from")
    revision = ""
    for ref in (f"refs/remotes/origin/{curate_runtime._branch(root)}", "HEAD"):
        revision = curate_runtime._git(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").stdout.strip()
        if revision:
            break
    if at is not None and revision:
        earlier = curate_runtime._git(root, "rev-parse", "--verify", "-q", f"{at}^{{commit}}").stdout.strip() \
            if _REVISION.fullmatch(at) else ""
        if not earlier or curate_runtime._git(root, "merge-base", "--is-ancestor", earlier, revision).returncode:
            raise HTTPException(status_code=404, detail=f"{at} is not a published revision of this bundle")
        revision = earlier
    prefix = os.path.relpath(bundle.resolve(), root.resolve())
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", revision if prefix == "." else f"{revision}:{prefix}"],
        capture_output=True, timeout=curate_runtime.GIT_TIMEOUT_S,
    ) if revision else None
    if archive is None or archive.returncode != 0:
        raise HTTPException(status_code=503, detail="the published revision could not be read; retry")
    return root, revision, archive.stdout


def _tree(archive: bytes) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(archive))


def _unpublished(member: tarfile.TarInfo) -> bool:
    return member.name == "viz.html"  # generated on the writer; never part of a workspace


@app.get("/workspace")
def workspace(bundle: str | None = None, revision: str | None = None, if_none_match: str | None = Header(default=None),
              authorization: str | None = Header(default=None)):
    """The published bundle, sources/ included and viz.html left out, as a gzipped tar.

    ``X-AIWiki-Revision`` (also the ETag) names the commit; ``If-None-Match`` on it is a 304.
    ``revision`` asks for an earlier published commit instead; that needs the admin scope of a
    named principal, since history still holds what a revert or a removal took out of the
    bundle, and a shared legacy token (every scope, no actor) is in every member's hands.
    """
    principal = _auth(authorization, "read" if revision is None else "admin")
    _enabled("workspace")
    if revision is not None:
        _enabled("admin")
        if principal.actor is None:
            raise HTTPException(status_code=403, detail=f"{principal.id} has no actor; an earlier revision "
                                                        "needs a named admin principal")
    _name, path = _resolve(bundle, principal)
    _root, revision, archive = _published(path, revision)
    headers = {"X-AIWiki-Revision": revision, "ETag": f'"{revision}"'}
    if if_none_match and revision in {tag.strip().removeprefix("W/").strip('"') for tag in if_none_match.split(",")}:
        return Response(status_code=304, headers=headers)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as packed, tarfile.open(fileobj=packed, mode="w") as target, \
            _tree(archive) as source:
        for member in source:
            if not _unpublished(member):
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
    return Response(content=out.getvalue(), media_type="application/gzip", headers=headers)


def _evidence(path: Path, request: dict) -> list[changeset.EvidenceFile]:
    """The frozen bytes of ``evidence.item_files`` from the work-item store."""
    files = []
    for name in request["evidence"].get("item_files") or []:
        item_id, file = name.split("/", 1)
        try:
            meta, data = M.evidence(path, item_id, file)
        except M.MaintError as exc:
            if exc.status != 404:
                raise HTTPException(status_code=exc.status, detail=exc.detail()) from None
            raise _Rejected(changeset._rejected({}, [changeset._error(
                "input", f"evidence {name} is not a frozen file of its work item")])) from None
        files.append(changeset.EvidenceFile(name, data, meta.get("origin") or {}))
    return files


def _instant(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _throttle(principal: auth.Principal, request: dict) -> None:
    """G2: the principal's changeset and deprecation quotas over all its bundles, else 429."""
    now = datetime.now(UTC)
    jobs = [job for name, path in _registry().items() if principal.allows(name)
            for job in I.changeset_jobs(path, principal.id)]
    wanted = {"changesets_per_hour": 1, "changesets_per_day": 1,
              "deprecations_per_day": sum(entry.get("op") == "deprecate" for entry in request["files"])}
    for name, (window, env, default) in QUOTAS.items():
        if not wanted[name]:
            continue
        limit = principal.limits.get(name, int(os.environ.get(env, default)))
        used = []
        for job in jobs:
            created = _instant(job.get("created"))
            if created is None or created <= now - window:
                continue
            if name != "deprecations_per_day":
                used.append((created, 1))
            elif job.get("status") not in ("rejected", "failed") and job.get("deprecations"):
                used.append((created, int(job["deprecations"])))  # rejected and failed deprecated nothing
        excess = sum(count for _created, count in used) + wanted[name] - limit
        if excess > 0:
            # Retry once enough of the oldest uses have aged out of the window for this one to fit.
            ages_out = now
            for created, count in sorted(used):
                excess, ages_out = excess - count, created
                if excess <= 0:
                    break
            retry = max(1, int((ages_out + window - now).total_seconds()))
            message = f"{name} quota of {limit} is used up"
            raise _Rejected({"status": "rejected", "http_status": 429,
                             "errors": [{"code": "rate_limited", "limit": name, "message": message}],
                             "failure": {**failure("capacity", stage="intake", detail=message),
                                         "retry_after_s": retry}},
                            headers={"Retry-After": str(retry)})


def _check_items(path: Path, request: dict, principal: str, run: str | None) -> None:
    """G3b: every work item exists and is open, and the caller's run holds the maintainer lease.
    Like every write call of a run, the changeset renews the leases that run holds."""
    items = []
    for item_id in request.get("work_items") or []:
        try:
            items.append(M.get_item(path, item_id))
        except M.MaintError as exc:
            if exc.status != 404:
                raise HTTPException(status_code=exc.status, detail=exc.detail()) from None
            raise _Rejected(changeset._rejected({}, [changeset._error("input", f"no such work item: {item_id}")])) \
                from None
    closed = [item for item in items if item["status"] in M.TERMINAL]
    if closed:
        raise _Rejected(worker.closed_rejection(closed))
    if items:
        try:
            M.require_lease(path, "maintainer", principal=principal, run=run)
        except M.MaintError as exc:
            raise _Rejected(changeset._rejected({}, [changeset._error("lease_required", str(exc), **exc.extra)])) \
                from None
    else:
        M.renew(path, principal=principal, run=run)


def _dry_run(path: Path, request: dict, actor: str, evidence: list[changeset.EvidenceFile]) -> JSONResponse:
    """Judge the request against the published revision in a scratch copy; write nothing."""
    root, revision, archive = _published(path)
    with tempfile.TemporaryDirectory(prefix="aiwiki-dry-run-") as scratch:
        tree = Path(scratch) / "bundle"
        with _tree(archive) as source:
            for member in source:
                if not _unpublished(member):
                    try:
                        source.extract(member, tree, filter="data")
                    except tarfile.FilterError:
                        continue  # a link out of the tree: never follow it
        if curate_runtime._git(root, "merge-base", "--is-ancestor", request["base_revision"], revision).returncode:
            result = changeset._rejected({"warnings": [], "validation": {"status": "not_run"}}, [changeset._error(
                "unknown_base", "base_revision is not an ancestor of the published branch",
                hint="workspace pull, then propose again from the published revision")])
        else:
            result = changeset.evaluate(tree, request, actor=actor, now=datetime.now(UTC), evidence_files=evidence)
        diffs = {}
        for rel, after in (result.pop("files", None) or {}).items():
            before = (tree / rel).read_text(encoding="utf-8") if (tree / rel).is_file() else ""
            diffs[rel] = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                                      f"a/{rel}", f"b/{rel}"))
    result.update(dry_run=True, published_revision=revision, diffs=diffs)
    return JSONResponse(status_code=result["http_status"] if result["status"] == "rejected" else 200, content=result)


def _receipt(job: dict, deduplicated: bool, bundle: str) -> JSONResponse:
    """A changeset job as the HTTP answer of design §2.8."""
    status, headers = job.get("status"), {}
    if status in ("queued", "running"):
        code = 202
        headers["Location"] = f"/jobs/{job['id']}?bundle={quote(bundle)}"
    elif status == "done":
        code = 200 if deduplicated or job.get("noop") else 201
    elif status == "rejected":
        code = int(job.get("http_status") or 422)
    else:
        cause = job.get("failure") if isinstance(job.get("failure"), dict) else {}
        code = 503 if cause.get("class") in ("transient", "capacity", "timeout", "interrupted") else 500
        if code == 503:
            headers["Retry-After"] = str(cause.get("retry_after_s") or 60)
    return JSONResponse(status_code=code, content={**job, "deduplicated": deduplicated}, headers=headers)


async def _raw_body(request: Request) -> bytes:
    """The body unparsed, so a malformed one is a G1 400 after the token check, not a 422."""
    return await request.body()


@app.post("/changesets")
def changesets(raw: bytes = Depends(_raw_body), bundle: str | None = None, dry_run: bool = False,
               x_aiwiki_run: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    """Propose a curate changeset: G0–G4 here, G5–G15 on the serial worker (design §2.5).

    Answers with the receipt once it is final, or 202 with ``Location`` after
    AIWIKI_CHANGESET_WAIT_S. ``dry_run`` judges the published revision and writes nothing.
    Only bundles in AIWIKI_CHANGESETS_COMMIT commit; every bundle may dry-run.
    """
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None  # G1 answers: a changeset must be a JSON object
    kind = body.get("kind") if isinstance(body, dict) else None
    principal = _auth(authorization, *_CHANGESET_SCOPES.get(kind if isinstance(kind, str) else "",
                                                            ("curate", "audit", "admin")))
    _enabled("changesets")
    if not dry_run and not CURATE_ON:
        raise HTTPException(status_code=403, detail="committing a changeset requires the writer (AIWIKI_CURATE)")
    if principal.actor is None:
        raise HTTPException(status_code=403, detail=f"{principal.id} has no actor to stamp as generated.by")
    try:
        if dry_run:
            _name, path = _resolve(bundle, principal)
            body, evidence = _intake(path, body, x_aiwiki_run, principal)
            _throttle(principal, body)
            return _dry_run(path, body, principal.actor, evidence)
        with worker.serialized_lifecycle():  # dedup and create atomically, never in a deleted bundle
            name, path = _resolve(bundle, principal)
            if name not in MODES["changesets_commit"]:
                raise HTTPException(status_code=403, detail=f"bundle '{name}' takes dry-run changesets only "
                                                            "(AIWIKI_CHANGESETS_COMMIT)")
            body, evidence = _intake(path, body, x_aiwiki_run, principal)
            digest = changeset.changeset_sha256(body, {item.name: hashlib.sha256(item.data).hexdigest()
                                                       for item in evidence})
            job = I.find_changeset_job(path, principal.id, digest)
            deduplicated = job is not None
            if job is None:  # a resend after a lost answer returns its receipt, never a 429 or 409
                _throttle(principal, body)
                _check_items(path, body, principal.id, body.get("run"))
                record = {"request": body, "evidence_files": [
                    {"name": item.name, "sha256": hashlib.sha256(item.data).hexdigest(), "origin": dict(item.origin)}
                    for item in evidence]}
                # base_revision becomes the head the writer applies it on (what recovery resets
                # to); the authored base is kept apart, so a file-level rebase shows (§2.6).
                job = I.new_changeset_job(
                    path, record, principal=principal.id, actor=principal.actor, run=body.get("run"),
                    request_base_revision=body["base_revision"],
                    work_items=list(body.get("work_items") or []), close_items=body.get("close_items", True),
                    changeset_sha256=digest, deprecations=sum(file.get("op") == "deprecate" for file in body["files"]),
                )
                worker.ensure_started()
                worker.submit_changeset(path, I.job_path(path, job["id"]), principal=principal.id, digest=digest)
    except _Rejected as rejected:
        return JSONResponse(status_code=rejected.result["http_status"], content=rejected.result,
                            headers=rejected.headers)
    if job["status"] in ("queued", "running") and not deduplicated:  # a resend polls its Location instead
        worker.wait(I.job_path(path, job["id"]), WAIT_S)
        job = I.read_job(path, job["id"]) or job
    return _receipt(job, deduplicated, name)


def _intake(path: Path, request: object, header_run: str | None,
            principal: auth.Principal) -> tuple[dict, list[changeset.EvidenceFile]]:
    """G1 (schema, limits, paths, the evidence packet) and the frozen evidence it names.

    The request comes back with its effective ``run`` (X-AIWiki-Run, else the body's), so the
    lease check, the receipt, the gate's secret scan and the commit trailer all name one run.
    Only a human uploads a packet (design §2.2 rule 1, §5.6): a process cites frozen item
    files, so text an agent wrote never becomes evidence.
    """
    errors = changeset.check_request(request)
    if errors:
        raise _Rejected(changeset._rejected({}, errors))
    if request["kind"] != "curate":
        raise _Rejected(changeset._rejected({}, [changeset._error(
            "input", "audit changesets are not accepted until the audit gate is enabled")]))
    if request["evidence"].get("upload") is not None and not principal.id.startswith("human:"):
        raise HTTPException(status_code=403, detail=f"{principal.id} may not upload evidence; cite the frozen "
                                                    "files of a work item (evidence.item_files)")
    run = header_run or request.get("run")
    if run is not None and (not M._RUN.fullmatch(run) or request.get("run", run) != run):
        raise _Rejected(changeset._rejected({}, [changeset._error(
            "input", "X-AIWiki-Run and run must name the same run (1-128 of A-Z a-z 0-9 _ . : @ / -)")]))
    if run is not None and secrets.scan(run):  # the receipt shows the run to every reader
        raise _Rejected(changeset._rejected({}, [changeset._error("secret_detected", "run carries a secret")]))
    evidence = _evidence(path, request)
    _packet, errors = changeset.build_packet(request["evidence"], evidence)
    if errors:
        raise _Rejected(changeset._rejected({}, errors))
    return ({**request, "run": run} if run else request), evidence


# --- maintenance state: leases, cursors and work items (design §2.10) -------------------
# Writes serialize on maint_state's own lock, never on the curation mutation lock, so a
# long Codex pass does not stall the maintainer loop. Principals and scopes come from auth.py.


@contextmanager
def _maint(bundle: str | None, authorization: str | None, scope: str, *, area: str = "maint",
           write: bool = False):
    """Authorize, resolve the bundle inside the lifecycle window, and map state errors to HTTP.

    The state lives on the writer's disk, so a read deployment (AIWIKI_CURATE=off) refuses
    rather than answering from an empty or read-only ``.okf/maint``. A ``write`` needs a
    principal with an actor: a shared member token never holds a run, feeds the queue or
    closes an item under nobody's name.
    """
    principal = _auth(authorization, scope)
    _enabled(area)
    if not CURATE_ON:
        raise HTTPException(status_code=403, detail=f"/{area} requires the writer (AIWIKI_CURATE enabled)")
    if write and principal.actor is None:
        raise HTTPException(status_code=403, detail=f"{principal.id} has no actor; /{area} writes need a "
                                                    "process: or human: principal")
    try:
        with worker.serialized_lifecycle():  # a bundle being deleted never regains .okf/maint
            _name, path = _resolve(bundle, principal)
            yield path, principal.id
    except M.MaintError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from None


def _lease_scope(role: str) -> str:
    if role not in M.ROLES:
        raise HTTPException(status_code=404, detail=f"unknown lease role '{role}'")
    return M.ROLES[role]


@app.post("/maint/lease/{role}")
def maint_lease_acquire(role: str, bundle: str | None = None, x_aiwiki_run: str | None = Header(default=None),
                        authorization: str | None = Header(default=None)):
    """Take the role's run-level lease (TTL 3h); another live run gets 409 {holder, run, expires_at}."""
    with _maint(bundle, authorization, _lease_scope(role), write=True) as (path, principal):
        return M.acquire_lease(path, role, principal=principal, run=x_aiwiki_run)


@app.delete("/maint/lease/{role}")
def maint_lease_release(role: str, bundle: str | None = None, x_aiwiki_run: str | None = Header(default=None),
                        authorization: str | None = Header(default=None)):
    with _maint(bundle, authorization, _lease_scope(role), write=True) as (path, principal):
        return M.release_lease(path, role, principal=principal, run=x_aiwiki_run)


@app.get("/maint/cursors/{name}")
def maint_cursor_get(name: str, response: Response, bundle: str | None = None,
                     authorization: str | None = Header(default=None)):
    with _maint(bundle, authorization, "read") as (path, _principal_id):
        cursor = M.get_cursor(path, name)
    response.headers["ETag"] = f'"{cursor["etag"]}"'
    return cursor


@app.put("/maint/cursors/{name}")
def maint_cursor_put(name: str, response: Response, body: dict, bundle: str | None = None,
                     if_match: str | None = Header(default=None), if_none_match: str | None = Header(default=None),
                     x_aiwiki_run: str | None = Header(default=None),
                     authorization: str | None = Header(default=None)):
    """Compare-and-swap a collector cursor: body ``{value}``; If-Match or If-None-Match: * required."""
    with _maint(bundle, authorization, "curate", write=True) as (path, principal):
        cursor = M.put_cursor(path, name, body.get("value"), if_match=if_match, if_none_match=if_none_match,
                              principal=principal, run=x_aiwiki_run)
    response.headers["ETag"] = f'"{cursor["etag"]}"'
    return cursor


@app.post("/maint/items")
def maint_items_enqueue(body: dict, bundle: str | None = None,
                        x_aiwiki_run: str | None = Header(default=None),
                        authorization: str | None = Header(default=None)):
    """Enqueue collected items with their evidence bytes; idempotent by item_key, merged by topic."""
    with _maint(bundle, authorization, "curate", write=True) as (path, principal):
        return M.enqueue(path, body.get("items"), principal=principal, run=x_aiwiki_run)


@app.get("/maint/items")
def maint_items_list(status: str | None = None, origin: str | None = None,
                     limit: int = Query(default=50, ge=1, le=1000), bundle: str | None = None,
                     authorization: str | None = Header(default=None)):
    with _maint(bundle, authorization, "read") as (path, _principal_id):
        return M.list_items(path, status=status, origin=origin, limit=limit)


@app.post("/maint/items/next")
def maint_items_next(bundle: str | None = None, x_aiwiki_run: str | None = Header(default=None),
                     authorization: str | None = Header(default=None)):
    """Claim the highest-priority ready item for the lease-holding run (``item`` is null when empty)."""
    with _maint(bundle, authorization, "curate", write=True) as (path, principal):
        return M.next_item(path, principal=principal, run=x_aiwiki_run)


@app.get("/maint/items/{item_id}")
def maint_item_get(item_id: str, bundle: str | None = None, authorization: str | None = Header(default=None)):
    with _maint(bundle, authorization, "read") as (path, _principal_id):
        return M.get_item(path, item_id)


@app.post("/maint/items/{item_id}/files")
def maint_item_add_file(item_id: str, body: dict, bundle: str | None = None,
                        x_aiwiki_run: str | None = Header(default=None),
                        authorization: str | None = Header(default=None)):
    """Freeze one more evidence file ``{name, content_b64, origin}`` into the run's item."""
    with _maint(bundle, authorization, "curate", write=True) as (path, principal):
        return M.add_file(path, item_id, body, principal=principal, run=x_aiwiki_run)


@app.get("/maint/items/{item_id}/files/{name}")
def maint_item_file(item_id: str, name: str, bundle: str | None = None,
                    authorization: str | None = Header(default=None)):
    with _maint(bundle, authorization, "read") as (path, _principal_id):
        data = M.read_file(path, item_id, name)
    return Response(content=data, media_type="application/octet-stream")


@app.post("/maint/items/{item_id}/resolve")
def maint_item_resolve(item_id: str, body: dict, bundle: str | None = None,
                       x_aiwiki_run: str | None = Header(default=None),
                       authorization: str | None = Header(default=None)):
    """``{outcome: skipped|duplicate|needs_access|needs_conversion|parked|split, reason, class?, children?}``."""
    with _maint(bundle, authorization, "curate", write=True) as (path, principal):
        return M.resolve(path, item_id, body, principal=principal, run=x_aiwiki_run)


@app.get("/maint/status")
def maint_status(bundle: str | None = None, authorization: str | None = Header(default=None)):
    """SLO snapshot: cursor ages, item counts and oldest ages, needs_human, leases, audit backlog."""
    with _maint(bundle, authorization, "read") as (path, _principal_id):
        return M.status(path)


@app.post("/admin/items/{item_id}/retry")
def admin_item_retry(item_id: str, body: dict | None = None, bundle: str | None = None,
                     authorization: str | None = Header(default=None)):
    """Reopen a needs_human (or skipped/parked/…) item as ready with fresh attempt counters."""
    with _maint(bundle, authorization, "admin", area="admin", write=True) as (path, principal):
        reason = (body or {}).get("reason")
        return M.admin_retry(path, item_id, principal=principal, reason=reason if isinstance(reason, str) else None)


@app.post("/admin/items/{item_id}/resolve")
def admin_item_resolve(item_id: str, body: dict, bundle: str | None = None,
                       authorization: str | None = Header(default=None)):
    with _maint(bundle, authorization, "admin", area="admin", write=True) as (path, principal):
        return M.admin_resolve(path, item_id, body, principal=principal)


# --- incident response: what a principal changed, and reverting it (design §8.5) --------------

_ROW = ("id", "status", "principal", "actor", "run", "created", "finished", "commit", "noop", "work_items",
        "concept_files", "deprecated_files", "changeset_sha256")
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _since(value: object) -> datetime | None:
    """An ISO 8601 date or time, UTC unless it names a zone; anything else is a 400."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="since must be an ISO 8601 date or time") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _changesets(path: Path, principal: str | None, since: datetime | None) -> list[dict]:
    """The bundle's changeset jobs newest first, of one principal and created at ``since`` or later."""
    jobs = [job for job in I.changeset_jobs(path, principal)
            if since is None or ((created := _instant(job.get("created"))) is not None and created >= since)]
    return sorted(jobs, key=lambda job: (str(job.get("created") or ""), str(job.get("id"))), reverse=True)


@app.get("/admin/changesets")
def admin_changesets(principal: str | None = None, since: str | None = None,
                     limit: int = Query(default=100, ge=1, le=1000), bundle: str | None = None,
                     authorization: str | None = Header(default=None)):
    """Changesets newest first, by principal and ``since``, each with the revert that undid it."""
    with _maint(bundle, authorization, "admin", area="admin") as (path, _admin):
        jobs, reverted = _changesets(path, principal, _since(since)), I.reverted_by(path)
    rows = [{**{key: job.get(key) for key in _ROW}, "reverted_by": reverted.get(job.get("id"))} for job in jobs]
    return {"changesets": rows[:limit], "shown": min(limit, len(rows)), "total": len(rows),
            "truncated": len(rows) > limit}


def _revert_request(body: dict, run: str | None) -> tuple[str | None, str | None, str | None]:
    """``(changeset, principal, reason)`` of a revert request, else a 400."""
    changeset_id, principal, reason = (body.get(key) for key in ("changeset", "principal", "reason"))
    if (changeset_id is None) == (principal is None) or (principal is None) != (body.get("since") is None):
        raise HTTPException(status_code=400, detail="name a changeset, or a principal and since")
    if changeset_id is not None and not (isinstance(changeset_id, str) and _JOB_ID.fullmatch(changeset_id)):
        raise HTTPException(status_code=400, detail="changeset must be a changeset job id")
    if principal is not None and not (isinstance(principal, str) and 0 < len(principal) <= 200):
        raise HTTPException(status_code=400, detail="principal must be a principal id")
    if reason is not None and not (isinstance(reason, str) and len(reason) <= 500):
        raise HTTPException(status_code=400, detail="reason must be a string of at most 500 characters")
    if reason is not None and secrets.scan(reason):  # the receipt shows the reason to every reader
        raise HTTPException(status_code=400, detail="reason carries a secret; describe it without the value")
    if run is not None and (not M._RUN.fullmatch(run) or secrets.scan(run)):
        raise HTTPException(status_code=400, detail="X-AIWiki-Run must name the run (1-128 of A-Z a-z 0-9 _ . : @ / -)")
    return changeset_id, principal, reason


@app.post("/admin/revert")
def admin_revert(body: dict, bundle: str | None = None, x_aiwiki_run: str | None = Header(default=None),
                 authorization: str | None = Header(default=None)):
    """Revert committed changesets on the writer, newest first, stopping at the first conflict.

    Body ``{changeset: <job id>}`` or ``{principal: <id>, since: <ISO time>}``, and an optional
    ``reason``. Answers as POST /changesets does: 201 committed (``stopped`` names a changeset
    the revert could not step over: a conflict, or a problem its undo would leave), 202 with
    ``Location`` while it queues, 409 when the newest changeset already conflicts, 422 when
    undoing the newest already breaks the bundle, 503 on a stale base. A changeset already
    reverted, or held by a queued revert, is never reverted twice; a resend meanwhile gets the
    receipt of the revert that holds it.
    """
    # A shared legacy token never reverts anonymously: the revert commit names its actor.
    with _maint(bundle, authorization, "admin", area="admin", write=True) as (path, admin):
        actor = admin
        changeset_id, principal, reason = _revert_request(body, x_aiwiki_run)
        since = _since(body.get("since"))
        reverted = I.reverted_by(path)
        running = {entry.get("id"): job.get("id") for job in I.revert_jobs(path)
                   if job.get("status") in ("queued", "running")
                   for entry in job.get("changesets") or [] if isinstance(entry, dict)}
        claimed = reverted | running
        if changeset_id in claimed:  # a resend after a lost answer gets the revert it started
            return _receipt(I.read_job(path, claimed[changeset_id]) or {}, True, path.name)
        if changeset_id is not None:
            found = I.read_job(path, changeset_id)
            if not isinstance(found, dict) or found.get("mode") != "changeset":
                raise HTTPException(status_code=404, detail=f"no such changeset: {changeset_id}")
            candidates = [found]
        else:
            candidates = _changesets(path, principal, since)
        targets = [job for job in candidates
                   if job.get("status") == "done" and job.get("commit") and job.get("id") not in claimed]
        if changeset_id is not None and not targets:
            raise HTTPException(status_code=409, detail=f"changeset {changeset_id} committed nothing to revert "
                                                        f"(status {candidates[0].get('status')})")
        waiting = next((running[job["id"]] for job in candidates if job.get("id") in running), None)
        if not targets and waiting:  # a resend while its revert still runs polls that revert
            return _receipt(I.read_job(path, waiting) or {}, True, path.name)
        if not targets:
            return {"status": "noop", "changesets": [], "deduplicated": False,
                    "reverted_by": {job["id"]: reverted[job["id"]] for job in candidates if job.get("id") in reverted}}
        job = I.new_revert_job(
            path, principal=admin, actor=actor, run=x_aiwiki_run, reason=reason,
            selection={"changeset": changeset_id} if changeset_id else {"principal": principal,
                                                                       "since": since.isoformat()},
            changesets=[{"id": job["id"], "commit": job["commit"], "principal": job.get("principal")}
                        for job in targets])
        worker.ensure_started()
        worker.submit_revert(path, I.job_path(path, job["id"]))
    worker.wait(I.job_path(path, job["id"]), WAIT_S)
    return _receipt(I.read_job(path, job["id"]) or job, False, path.name)
