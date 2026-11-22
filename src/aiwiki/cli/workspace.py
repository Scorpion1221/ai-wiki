"""Local curation verbs: pull the published bundle, judge local edits, propose them (design §3).

A workspace is a directory holding one bundle's published tree plus ``.ai-wiki/``:
``workspace.json`` ``{bundle, base_revision, actor, hashes[, discard]}``, ``base/`` (the
pulled tree, never edited), ``items/`` (frozen work-item evidence fetched for
``--item``) and ``lock`` (held while a pull or a changeset's build and verdict runs: see
``_locked``). Every file that differs from ``base/`` is part of the changeset. ``validate``
judges it with ``runtime.changeset.evaluate`` on ``base/``, the code the server's dry-run
runs, so both reach one verdict in one format; ``propose`` sends it to ``POST /changesets``.

Exit codes: 0 ok, 1 an unexpected failure, 2 usage, 4 preflight or authorization, 6 rejected,
7 conflict, 8 no final answer after retries (the item is parked), 11 rate limited.
"""
from __future__ import annotations

import base64
import contextlib
import difflib
import fcntl
import hashlib
import io
import json
import os
import shutil
import stat as stat_mod
import tarfile
import threading
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import yaml

from aiwiki.cli import main as cli
from aiwiki.cli.toon import emit, object_lines, table_lines
from aiwiki.engine.document import OKFDocumentError, parse_document
from aiwiki.runtime import changeset

META = ".ai-wiki"
MINE = ".mine"
_STAT_CACHE = "stat-cache.json"
_LAST_VALIDATED = "last-validated.json"
_RACY_NS = 2_000_000_000  # 2 s racy-clean guard, like Git: recently written files are always re-hashed
_VALIDATION_TTL_S = 900
PLACEHOLDER = b"ai-wiki local validation placeholder\n"  # stands in for a packet only when judging locally
LOCK_WAIT_S = 900  # longer than a pull holds the lock: GET /workspace gives up within 2 x 300 + 20 s
RETRY_DELAYS_S = (5, 15, 30)  # resends of the same bytes after an answer that settles nothing
POLL_S = 5
OK, USAGE, PREFLIGHT, REJECTED, CONFLICT, TRANSIENT, RATE_LIMITED = 0, 2, 4, 6, 7, 8, 11


class WorkspaceError(Exception):
    """A verb that cannot run; ``code`` is its exit code."""

    def __init__(self, message: str, code: int = USAGE):
        super().__init__(message)
        self.code = code


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(body: bytes) -> dict:
    try:
        value = json.loads(body)
    except ValueError:
        value = None
    return value if isinstance(value, dict) else {"detail": body.decode("utf-8", errors="replace")[:500]}


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _failed(what: str, status: int, body: bytes) -> WorkspaceError:
    detail = _json(body).get("detail")
    return WorkspaceError(f"{what} answered {status}" + (f": {detail}" if isinstance(detail, str) else ""),
                          PREFLIGHT if status in (401, 403) else 1)


# --- the workspace on disk --------------------------------------------------------------


def _files(root: Path, *, workspace: bool) -> dict[str, Path]:
    """Regular files under ``root`` by bundle path. Symlinks are never read, so none can
    carry a file from outside the tree into a changeset; a workspace skips its own state."""
    found: dict[str, Path] = {}
    for directory, dirnames, filenames in os.walk(root):
        base = Path(directory)
        if workspace and base == root:
            dirnames[:] = [name for name in dirnames if name not in (META, ".git")]
        for name in filenames:
            path = base / name
            if path.is_symlink() or not path.is_file() or (workspace and name.endswith(MINE)):
                continue
            found[path.relative_to(root).as_posix()] = path
    return found


def _load_stat_cache(meta: Path) -> dict[str, dict[str, list]]:
    try:
        raw = json.loads((meta / _STAT_CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"work": {}, "base": {}}
    if not isinstance(raw, dict) or raw.get("v") != 1:
        return {"work": {}, "base": {}}
    work = raw.get("work") if isinstance(raw.get("work"), dict) else {}
    base = raw.get("base") if isinstance(raw.get("base"), dict) else {}
    return {"work": dict(work), "base": dict(base)}


def _save_stat_cache(meta: Path, cache: dict[str, dict[str, list]]) -> None:
    if not (meta / "workspace.json").is_file():
        return
    with contextlib.suppress(OSError):
        _write(meta / _STAT_CACHE, json.dumps(
            {"v": 1, "work": cache["work"], "base": cache["base"]}, separators=(",", ":")).encode("utf-8"))


def _stat_tuple(st: os.stat_result) -> list[int]:
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _hashes(root: Path, *, workspace: bool = False, cache: dict[str, list] | None = None,
            dirty: list[bool] | None = None) -> dict[str, str]:
    """SHA-256 of every regular non-symlink file under ``root``.

    When backed by a stat cache (automatic for initialized workspaces, or passed explicitly),
    files older than ``_RACY_NS`` whose ``(dev, ino, size, mtime_ns, ctime_ns)`` match are
    reused without re-reading disk, avoiding 280 MB+ re-hashes of unchanged ``sources/`` on
    every verb while always re-hashing recently written or modified files.
    """
    auto_cache: dict[str, dict[str, list]] | None = None
    if cache is None and workspace and (root / META / "workspace.json").is_file():
        auto_cache = _load_stat_cache(root / META)
        cache = auto_cache["work"]
        dirty = [False]
    now_ns = time.time_ns()
    found: dict[str, str] = {}
    if not root.is_dir():
        if cache:
            cache.clear()
            if dirty is not None:
                dirty[0] = True
        return found
    for directory, dirnames, filenames in os.walk(root):
        base = Path(directory)
        if workspace and base == root:
            dirnames[:] = [name for name in dirnames if name not in (META, ".git")]
        for name in filenames:
            if workspace and name.endswith(MINE):
                continue
            path = base / name
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat_mod.S_ISLNK(st.st_mode) or not stat_mod.S_ISREG(st.st_mode):
                continue
            rel = path.relative_to(root).as_posix()
            if cache is not None:
                key = _stat_tuple(st)
                cacheable = (now_ns - st.st_ctime_ns) > _RACY_NS
                cached = cache.get(rel)
                if cacheable and isinstance(cached, list) and len(cached) == 6 and cached[:5] == key \
                        and isinstance(cached[5], str):
                    found[rel] = cached[5]
                    continue
                sha = _sha(path.read_bytes())
                found[rel] = sha
                if cacheable:
                    try:
                        after = _stat_tuple(os.lstat(path))
                    except OSError:
                        after = None
                    if after == key:
                        cache[rel] = [*key, sha]
                        if dirty is not None:
                            dirty[0] = True
                elif rel in cache:
                    cache.pop(rel, None)
                    if dirty is not None:
                        dirty[0] = True
            else:
                found[rel] = _sha(path.read_bytes())
    if cache is not None:
        stale = cache.keys() - found.keys()
        if stale:
            for rel in stale:
                cache.pop(rel, None)
            if dirty is not None:
                dirty[0] = True
        if auto_cache is not None and dirty and dirty[0]:
            _save_stat_cache(root / META, auto_cache)
    return dict(sorted(found.items()))


def load(root: Path) -> dict:
    try:
        state = json.loads((root / META / "workspace.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise WorkspaceError(f"{root} is not an ai-wiki workspace; run: ai-wiki workspace pull --dir {root}") from None
    if not isinstance(state, dict) or not all(isinstance(state.get(key), kind) for key, kind in (
            ("bundle", str), ("base_revision", str), ("actor", (str, type(None))), ("hashes", dict),
            ("discard", (dict, type(None))))):
        raise WorkspaceError(f"{root / META / 'workspace.json'} is damaged; pull into a new directory")
    return state


def changes(root: Path, state: dict) -> tuple[dict[str, str], dict[str, str]]:
    """``({path: added|modified|deleted}, local hashes)`` of the workspace against its base."""
    local, base = _hashes(root, workspace=True), state["hashes"]
    changed = {}
    for rel in sorted(local.keys() | base.keys()):
        if rel not in base:
            changed[rel] = "added"
        elif rel not in local:
            changed[rel] = "deleted"
        elif local[rel] != base[rel]:
            changed[rel] = "modified"
    return changed, local


def conflicts(root: Path) -> list[str]:
    """Paths a pull left a ``.mine`` copy of: the agent's version, to re-apply to the server's."""
    return sorted(rel.removesuffix(MINE) for rel in (
        path.relative_to(root).as_posix() for path in root.rglob(f"*{MINE}") if path.is_file()
    ) if not rel.startswith(f"{META}/"))


def _extract(archive: bytes, target: Path) -> None:
    """Unpack a ``GET /workspace`` tar as the server's dry-run does, so both judge one tree."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        for member in tree:
            try:
                tree.extract(member, target, filter="data")
            except tarfile.FilterError:
                continue  # a link out of the tree: never follow it


@contextlib.contextmanager
def _locked(root: Path):
    """``<root>/.ai-wiki/lock``, held exclusively: one ai-wiki command at a time pulls the workspace
    or reads its base. Two at once (a command the agent's tool moved to the background, then the
    next one) unpacked into one ``incoming/``, and a pull deleted the workspace files the other's
    half tree lacked, then recorded their hashes (an audit round lost 32 concepts' bodies,
    2026-11-20). The kernel drops the lock with its process, so a killed command never holds it."""
    path = root / META / "lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WorkspaceError(f"{path} stayed held for {LOCK_WAIT_S} s by another ai-wiki command on "
                                         "this workspace; run this again once that command ends", TRANSIENT) from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def _actor() -> str | None:
    """The token's ``generated.by``; ``None`` for a member, whose changesets the writer refuses."""
    status, _headers, body = cli._http("GET", "/whoami")
    if status != 200:
        raise _failed("GET /whoami", status, body)
    return _json(body).get("actor") or None


def _prune_empty_dirs(directory: Path, *, stop: Path) -> None:
    stop_resolved = stop.resolve()
    current = directory
    while True:
        try:
            if current.resolve() == stop_resolved:
                return
        except OSError:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _safe_delta_path(rel: object) -> bool:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or rel.endswith("/"):
        return False
    parts = PurePosixPath(rel).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return False
    return parts[0] not in (META, ".git")


def _parse_delta(
    incoming: Path, expected_base: str, expected_revision: str
) -> tuple[list[str], list[str], dict[str, str]] | None:
    manifest_path = incoming / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        return None
    if manifest.get("base_revision") != expected_base or manifest.get("revision") != expected_revision:
        return None
    updated = manifest.get("updated")
    deleted = manifest.get("deleted")
    if not isinstance(updated, list) or not isinstance(deleted, list):
        return None
    if not all(_safe_delta_path(p) for p in updated) or not all(_safe_delta_path(p) for p in deleted):
        return None
    if set(updated) & set(deleted):
        return None
    tree_dir = incoming / "tree"
    delta_hashes = _hashes(tree_dir) if tree_dir.is_dir() else {}
    if set(delta_hashes) != set(updated):
        return None
    return updated, deleted, delta_hashes


def _clear_validation(root: Path) -> None:
    with contextlib.suppress(OSError):
        (root / META / _LAST_VALIDATED).unlink(missing_ok=True)


def _validation_fingerprint(state: dict, request: dict, evidence: Evidence) -> str:
    payload = {
        "base_revision": state["base_revision"],
        "actor": state["actor"],
        "day": datetime.now(UTC).strftime("%Y-%m-%d"),
        "request": request,
        "evidence": [[f.name, _sha(f.data), f.origin] for f in evidence],
        "answers": sorted(evidence.answers),
    }
    return _sha(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _record_validation(root: Path, state: dict, request: dict, evidence: Evidence, result: dict,
                       *, cacheable: bool) -> None:
    if not cacheable or result.get("status") not in ("would_apply", "noop") or not state.get("actor"):
        _clear_validation(root)
        return
    with contextlib.suppress(OSError):
        _write(root / META / _LAST_VALIDATED, json.dumps({
            "fingerprint": _validation_fingerprint(state, request, evidence),
            "at": time.time(),
            "status": result["status"],
        }, separators=(",", ":")).encode("utf-8"))


def _has_valid_cache(root: Path, state: dict, request: dict, evidence: Evidence) -> bool:
    if not state.get("actor"):
        return False
    try:
        cached = json.loads((root / META / _LAST_VALIDATED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(cached, dict) or cached.get("status") not in ("would_apply", "noop"):
        return False
    try:
        age = time.time() - float(cached.get("at", 0))
    except (TypeError, ValueError):
        return False
    if not 0 <= age <= _VALIDATION_TTL_S:
        return False
    return cached.get("fingerprint") == _validation_fingerprint(state, request, evidence)


def pull(root: Path, bundle: str) -> dict:
    """Bring the workspace to the published revision, keeping local edits the server did not touch.

    A local edit of a path the server changed too is moved to ``<path>.mine`` and the server's
    version takes its place (a conflict). A path ``propose`` committed (``discard`` in
    ``workspace.json``) that still holds the committed bytes takes the server's version, and
    its ``.mine`` goes. A ``.ai-wiki/`` without ``workspace.json`` is a first pull that never
    finished, so it is pulled into afresh. A second pull of the workspace waits for the first
    (``_locked``), then finds its result.
    """
    if not (root / META).is_dir() and root.exists() and any(root.iterdir()):
        raise WorkspaceError(f"{root} is not empty and not a workspace; pull into a new directory")
    # A first pull asks for the actor before it writes anything, the lock file included.
    actor = _ASK if (root / META / "workspace.json").exists() else _actor()
    with _locked(root):
        return _pull(root, bundle, actor)


_ASK = object()  # the actor is not known yet: ask the writer once the tree is downloaded


def _pull(root: Path, bundle: str, actor: str | None | object = _ASK) -> dict:
    meta, state = root / META, None
    if (meta / "workspace.json").exists():
        state = load(root)
        if state["bundle"] != bundle:
            raise WorkspaceError(f"{root} holds bundle {state['bundle']!r}, not {bundle!r}")
    stat_cache = _load_stat_cache(meta) if state else {"work": {}, "base": {}}
    cache_dirty = [False]
    if state:
        local = _hashes(root, workspace=True, cache=stat_cache["work"], dirty=cache_dirty)
        old_hashes = state["hashes"]
        changed = {}
        for rel in sorted(local.keys() | old_hashes.keys()):
            if rel not in old_hashes:
                changed[rel] = "added"
            elif rel not in local:
                changed[rel] = "deleted"
            elif local[rel] != old_hashes[rel]:
                changed[rel] = "modified"
    else:
        changed, local = {}, {}
    discard = (state or {}).get("discard") or {}
    base = _hashes(meta / "base", cache=stat_cache["base"], dirty=cache_dirty) if state else {}
    intact = state is not None and base == state["hashes"]  # a damaged base is downloaded again
    headers = {"If-None-Match": f'"{state["base_revision"]}"'} if intact else {}
    params = {"since": state["base_revision"]} if intact else None
    status, reply, body = cli._http("GET", "/workspace", bundle=bundle, params=params, headers=headers, timeout=300)
    incoming = None
    delta_parsed: tuple[list[str], list[str], dict[str, str]] | None = None
    if status == 304:
        revision, actor, new = state["base_revision"], state["actor"], base
    elif status == 200 and reply.get("X-AIWiki-Revision"):
        actor = _actor() if actor is _ASK else actor  # for local stamping; asked before the workspace changes
        revision, incoming = reply["X-AIWiki-Revision"], meta / "incoming"
        is_delta = (intact and reply.get("X-AIWiki-Workspace-Mode") == "delta"
                    and reply.get("X-AIWiki-Base-Revision") == state["base_revision"])
        shutil.rmtree(incoming, ignore_errors=True)
        _extract(body, incoming)
        if is_delta:
            delta_parsed = _parse_delta(incoming, state["base_revision"], revision)
            if delta_parsed is None:
                # Malformed/unexpected delta: fall back transparently to a full pull.
                shutil.rmtree(incoming, ignore_errors=True)
                status, reply, body = cli._http("GET", "/workspace", bundle=bundle, timeout=300)
                if status != 200 or not reply.get("X-AIWiki-Revision"):
                    raise _failed("GET /workspace", status, body)
                revision = reply["X-AIWiki-Revision"]
                _extract(body, incoming)
                new = _hashes(incoming)
            else:
                _upd, deleted_list, delta_hashes = delta_parsed
                new = dict(state["hashes"])
                for rel in deleted_list:
                    new.pop(rel, None)
                new.update(delta_hashes)
        else:
            new = _hashes(incoming)
    else:
        raise _failed("GET /workspace", status, body)
    old = (state or {}).get("hashes", {})
    updated, kept, conflicted = [], [], []
    if delta_parsed is not None:
        updated_list, deleted_list, delta_hashes = delta_parsed
        tree_dir = incoming / "tree"
        deletes: list[tuple[str, bool]] = []
        writes: list[tuple[str, bool]] = []
        for rel in sorted(delta_hashes.keys() | set(deleted_list) | changed.keys()):
            committed = rel in discard and discard[rel] == local.get(rel)
            edited = rel in changed and not committed and local.get(rel) != new.get(rel)
            if edited and old.get(rel) == new.get(rel):
                kept.append(rel)
                continue
            if rel not in changed and old.get(rel) == new.get(rel):
                continue
            if edited:
                conflicted.append(rel)
            else:
                updated.append(rel)
            if rel in new:
                writes.append((rel, edited))
            else:
                deletes.append((rel, edited))
        # Apply deletions before writes so file <-> directory transitions succeed cleanly.
        for rel, edited in deletes:
            target = root / rel
            if edited:
                if target.is_file():
                    os.replace(target, target.with_name(target.name + MINE))
            elif target.is_file():
                target.unlink()
                _prune_empty_dirs(target.parent, stop=root)
            stat_cache["work"].pop(rel, None)
        for rel, edited in writes:
            target = root / rel
            if edited and target.is_file():
                os.replace(target, target.with_name(target.name + MINE))
            target.parent.mkdir(parents=True, exist_ok=True)
            src = (tree_dir / rel) if rel in delta_hashes else (meta / "base" / rel)
            shutil.copyfile(src, target)
            stat_cache["work"].pop(rel, None)
        for rel, sha in discard.items():
            if local.get(rel) == sha:  # the conflict it resolved is committed
                (root / (rel + MINE)).unlink(missing_ok=True)
        base_dir = meta / "base"
        for rel in deleted_list:
            bpath = base_dir / rel
            bpath.unlink(missing_ok=True)
            _prune_empty_dirs(bpath.parent, stop=base_dir)
            stat_cache["base"].pop(rel, None)
        for rel in updated_list:
            bpath = base_dir / rel
            bpath.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tree_dir / rel, bpath)
            stat_cache["base"].pop(rel, None)
        shutil.rmtree(incoming, ignore_errors=True)
        cache_dirty[0] = True
    else:
        tree = incoming or meta / "base"
        deletes = []
        writes = []
        for rel in sorted(old.keys() | new.keys() | changed.keys()):
            committed = rel in discard and discard[rel] == local.get(rel)
            edited = rel in changed and not committed and local.get(rel) != new.get(rel)
            if edited and old.get(rel) == new.get(rel):
                kept.append(rel)
                continue
            if rel not in changed and old.get(rel) == new.get(rel):
                continue
            if edited:
                conflicted.append(rel)
            else:
                updated.append(rel)
            if rel in new:
                writes.append((rel, edited))
            else:
                deletes.append((rel, edited))
        for rel, edited in deletes:
            target = root / rel
            if edited:
                if target.is_file():
                    os.replace(target, target.with_name(target.name + MINE))
            elif target.is_file():
                target.unlink()
                _prune_empty_dirs(target.parent, stop=root)
            stat_cache["work"].pop(rel, None)
            cache_dirty[0] = True
        for rel, edited in writes:
            target = root / rel
            if edited and target.is_file():
                os.replace(target, target.with_name(target.name + MINE))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tree / rel, target)
            stat_cache["work"].pop(rel, None)
            cache_dirty[0] = True
        for rel, sha in discard.items():
            if local.get(rel) == sha:  # the conflict it resolved is committed
                (root / (rel + MINE)).unlink(missing_ok=True)
        if incoming:
            shutil.rmtree(meta / "base", ignore_errors=True)
            os.replace(incoming, meta / "base")
            stat_cache["base"].clear()
            cache_dirty[0] = True
    _write(meta / "workspace.json", json.dumps(
        {"bundle": bundle, "base_revision": revision, "actor": actor, "hashes": new}, indent=1).encode())
    if cache_dirty[0]:
        _save_stat_cache(meta, stat_cache)
    _clear_validation(root)
    return {"bundle": bundle, "base_revision": revision, "previous_revision": (state or {}).get("base_revision"),
            "up_to_date": status == 304, "updated": updated, "kept": kept, "conflicts": conflicted}


# --- the changeset ----------------------------------------------------------------------


def _base_hash(root: Path, rel: str) -> str | None:
    """The concept's CAS hash as the server computes it from the published bytes."""
    path = root / META / "base" / rel
    return changeset.content_hash(path.read_bytes().decode("utf-8", errors="replace")) if path.is_file() else None


def _cited(files: list[dict]) -> list[str]:
    """Evidence ids the changed concepts cite as ``evidence:packet``."""
    ids = set()
    for entry in files:
        try:
            sources = parse_document(entry.get("content") or "").frontmatter.get("sources")
        except (OKFDocumentError, ValueError, TypeError, AttributeError, LookupError):
            continue  # the gate reports the YAML
        for source in sources if isinstance(sources, list) else []:
            if isinstance(source, dict) and source.get("resource") == changeset.PACKET_RESOURCE:
                ids.add(str(source.get("id")))
    return sorted(ids)


class Evidence(list):
    """The frozen files a changeset cites. ``answers`` lists the concept paths its attention
    items name (``changeset.answered_paths`` of the items as the writer serves them), so the
    local gate exempts from the source cap exactly the pages the writer will."""

    def __init__(self, files=(), answers=()) -> None:
        super().__init__(files)
        self.answers: list[str] = list(answers)


def _item_evidence(root: Path, bundle: str, item_id: str, *,
                   item_out: dict | None = None) -> tuple[list[changeset.EvidenceFile], str | None]:
    """The work item's frozen files, fetched once into ``.ai-wiki/items/<id>/`` and checked by
    sha, and the run that claimed it. ``item_out`` receives the item as the writer serves it."""
    if not changeset._WORK_ITEM.fullmatch(item_id):
        raise WorkspaceError(f"{item_id!r} is not a work item id (it_<id>)")
    status, _headers, body = cli._http("GET", f"/maint/items/{item_id}", bundle=bundle)
    if status != 200:
        raise _failed(f"GET /maint/items/{item_id}", status, body)
    item, files = _json(body), []
    if item_out is not None:
        item_out.update(item)
    for entry in item.get("files") or []:
        name, sha = entry.get("name"), entry.get("sha256")
        if not isinstance(name, str) or not changeset._ITEM_FILE.fullmatch(f"{item_id}/{name}"):
            raise WorkspaceError(f"work item {item_id} names an unusable file {name!r}", 1)
        cached = root / META / "items" / item_id / name
        data = cached.read_bytes() if cached.is_file() else b""
        if _sha(data) != sha:
            status, _headers, data = cli._http("GET", f"/maint/items/{item_id}/files/{quote(name, safe='')}",
                                               bundle=bundle)
            if status != 200 or _sha(data) != sha:
                raise WorkspaceError(f"evidence {item_id}/{name} could not be fetched intact ({status})", 1)
            _write(cached, data)
        files.append(changeset.EvidenceFile(f"{item_id}/{name}", data, entry.get("origin") or {}))
    return files, item.get("current_run")


def build(root: Path, state: dict, *, item: str | list[str] | None = None, upload: Path | None = None,
          source_id: str | None = None, deprecate=(), retire=(), allow_shrink=(), allow_retype=(), allow_grow=(),
          close: bool = True, run: str | None = None) -> tuple[dict, Evidence]:
    """The changeset of the workspace's local changes and the frozen evidence it names.

    Without ``item`` or ``upload`` a placeholder packet stands in: good for judging the
    concepts locally, never sent. An item's changeset runs under the run that claimed it
    unless ``run`` names another. Several items (a batch the run holds) make one changeset
    that cites, and closes, each of them.
    """
    items = [item] if isinstance(item, str) else list(dict.fromkeys(item or []))
    changed, _local = changes(root, state)
    files = []
    for rel, change in changed.items():
        if change == "deleted":
            files.append({"path": rel, "op": "delete"})  # the gate answers delete_forbidden
            continue
        try:
            content = (root / rel).read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"{rel} is not UTF-8 (byte {exc.start}); save it as UTF-8", REJECTED) from None
        files.append({"path": rel, "op": "put", "base": _base_hash(root, rel), "content": content})
    for path, successor, reason in deprecate:
        path = unicodedata.normalize("NFC", path)
        files.append({"path": path, "op": "deprecate", "base": _base_hash(root, path),
                      "superseded_by": successor, "reason": reason})
    for path, reason in retire:  # a deprecation that names no successor
        path = unicodedata.normalize("NFC", path)
        files.append({"path": path, "op": "deprecate", "base": _base_hash(root, path), "reason": reason})
    if not files:
        raise WorkspaceError("nothing to propose: the workspace matches its base revision")
    if not source_id:
        cited = _cited(files)
        if len(cited) > 1:
            raise WorkspaceError(f"the changed concepts cite evidence:packet as {', '.join(cited)}; "
                                 "cite one id, or pass --source-id")
        source_id = cited[0] if cited else (items[0] if len(items) == 1 else "local-check")
    request: dict = {"schema": changeset.SCHEMA, "kind": "curate", "intent": "evidence",
                     "base_revision": state["base_revision"]}
    evidence = Evidence()
    if items:
        claimed = None
        for item_id in items:
            served: dict = {}
            frozen, holder = _item_evidence(root, state["bundle"], item_id, item_out=served)
            evidence.answers += changeset.answered_paths([served])  # the item's own origin, as the writer reads it
            # An attention notice or a triage record is no evidence (the gate refuses it): cite the
            # files beside it. One alone stays in, so the gate's answer tells the maintainer to add evidence.
            evidence += [file for file in frozen if file.origin.get("kind") not in changeset.NON_EVIDENCE] or frozen
            claimed = claimed or holder
        run = run or claimed
        request.update(work_items=items, close_items=close,
                       evidence={"id": source_id, "item_files": [file.name for file in evidence]})
    else:
        try:
            data = upload.read_bytes() if upload else PLACEHOLDER
        except OSError as exc:
            raise WorkspaceError(f"cannot read {upload}: {exc.strerror or 'read failed'}") from None
        request["evidence"] = {"id": source_id, "upload": {
            "filename": upload.name if upload else "placeholder.md", "content_b64": base64.b64encode(data).decode()}}
    request["files"] = files
    allow = {name: [{"path": path, "reason": reason} for path, reason in entries]
             for name, entries in (("shrink", allow_shrink), ("retype", allow_retype), ("grow", allow_grow))
             if entries}
    if allow:
        request["allow"] = allow
    if run:
        request["run"] = run
    return request, evidence


def judge(root: Path, state: dict, request: dict, evidence: Evidence) -> dict:
    """The gate's verdict on ``request`` against the pulled base: G1, G6 and G8–G12, locally."""
    if not state["actor"]:  # the writer answers 403: it has no generated.by to stamp
        raise WorkspaceError("this token has no actor to stamp as generated.by, so the writer refuses its "
                             "changesets; use a process: or human: token", PREFLIGHT)
    return changeset.evaluate(root / META / "base", request, actor=state["actor"], now=datetime.now(UTC),
                              evidence_files=evidence, answers=evidence.answers)


def verdict_code(result: dict) -> int:
    if result.get("status") in ("would_apply", "noop"):
        return OK
    return CONFLICT if result.get("http_status") == 409 else REJECTED


# --- proposing --------------------------------------------------------------------------


def _status(job: dict) -> int:
    """The HTTP status a job's receipt answers with (as the writer's POST /changesets)."""
    status = job.get("status")
    if status in ("queued", "running"):
        return 202
    if status == "done":
        return 200
    if status == "rejected":
        return int(job.get("http_status") or 422)
    cause = job.get("failure") if isinstance(job.get("failure"), dict) else {}
    return 503 if cause.get("class") in ("transient", "capacity", "timeout", "interrupted") else 500


def _exit(status: int | None) -> int:
    if status in (200, 201):
        return OK
    if status in (400, 413, 422):
        return REJECTED
    if status == 409:
        return CONFLICT
    if status in (401, 403):
        return PREFLIGHT
    if status == 429:
        return RATE_LIMITED
    return TRANSIENT if status is None else 1


def _final(status: int, result: dict) -> bool:
    """Whether an answer settles the changeset. A 503, a proxy's 502/504/52x while the writer
    restarts, or a 5xx without a job receipt does not: the job may exist or not."""
    return status < 500 or (status == 500 and "id" in result)


def _poll(bundle: str, job_id: str, deadline: float) -> tuple[int | None, dict]:
    job: dict = {"id": job_id, "status": "queued"}
    while time.monotonic() < deadline:
        time.sleep(POLL_S)
        try:
            status, _headers, body = cli._http("GET", f"/jobs/{quote(job_id, safe='')}", bundle=bundle)
        except OSError:
            continue
        if status in (401, 403, 404):  # a revoked token or a lost job: waiting changes nothing
            return status, _json(body)
        if status == 200:
            job = _json(body)
            if job.get("status") not in ("queued", "running"):
                return _status(job), job
    return None, job


def submit(bundle: str, data: bytes, *, dry_run: bool = False, wait: float = 1800) -> tuple[int | None, dict]:
    """POST the changeset bytes; follow a 202 to its receipt; resend the same bytes (5/15/30s)
    after a network failure or an answer that settles nothing (``_final``). ``None`` means no
    final answer came."""
    deadline = time.monotonic() + wait
    status, result = None, {}
    for delay in (0, *RETRY_DELAYS_S):
        time.sleep(delay)
        try:
            status, _headers, body = cli._http("POST", "/changesets", bundle=bundle, data=data, timeout=120,
                                               params={"dry_run": "true"} if dry_run else None)
        except OSError as exc:
            status, result = None, {"error": f"network: {exc}"}
            continue
        result = _json(body)
        if status == 202:
            status, result = _poll(bundle, str(result.get("id")), deadline)
            if status is None:
                return None, result  # still queued: a later propose of the same content finds it
        if _final(status, result):
            return status, result
    return None, result


def _park(bundle: str, item: str, run: str | None, reason: str) -> dict:
    """Hand the item back to the queue as transient, so the run moves on (design §4.6)."""
    try:
        status, _headers, body = cli._http(
            "POST", f"/maint/items/{item}/resolve", bundle=bundle, headers={"X-AIWiki-Run": run} if run else None,
            data=json.dumps({"outcome": "parked", "class": "transient", "reason": reason}).encode())
    except OSError as exc:
        return {"parked": False, "detail": f"network: {exc}"}
    return {"parked": status == 200, **({} if status == 200 else {"detail": _json(body).get("detail", status)})}


def _revert(root: Path, files: list[dict]) -> list[str]:
    """Put a parked item's paths back to the pulled base, their ``.mine`` copies too, so the
    next item's changeset does not carry them (design §4.6)."""
    reverted = []
    with _locked(root):  # a base a pull is replacing would read as missing: the file would go
        for entry in files:
            if entry["op"] == "deprecate":
                continue  # never a local edit
            base, target = root / META / "base" / entry["path"], root / entry["path"]
            if base.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(base, target)
            else:
                target.unlink(missing_ok=True)
            target.with_name(target.name + MINE).unlink(missing_ok=True)
            reverted.append(entry["path"])
        _clear_validation(root)
    return reverted


def propose(root: Path, request: dict, evidence: Evidence, *, dry_run: bool = False,
            wait: float = 1800) -> tuple[int, dict]:
    """Judge locally, send, and on success pull the committed result into the workspace.

    ``submit`` resends the same bytes while no answer settles the changeset (design §2.7). A
    later propose of the same content sends its own request (its run, its bases), and the
    writer finds a lost answer's job by the changeset digest, so nothing is kept on disk.
    Without a final answer the item is parked and its edits reverted (exit 8). Once committed
    the answer is 0 even if the pull fails: ``discard`` in ``workspace.json`` makes the next
    pull take the committed bytes rather than set the edits aside as ``.mine``.
    """
    state = load(root)
    bundle, data = state["bundle"], json.dumps(request, ensure_ascii=False).encode("utf-8")
    if dry_run:  # the server's verdict is the authority; a dry-run writes nothing, so ask it directly
        status, result = submit(bundle, data, dry_run=True, wait=wait)
        return _exit(status), result
    with _locked(root):  # never judged on a base a pull is replacing
        state = load(root)
        if not state["actor"]:
            raise WorkspaceError("this token has no actor to stamp as generated.by, so the writer refuses its "
                                 "changesets; use a process: or human: token", PREFLIGHT)
        if not _has_valid_cache(root, state, request, evidence):
            local = judge(root, state, request, evidence)
            if local["status"] == "rejected":
                _clear_validation(root)
                return verdict_code(local), local  # never sent
        _clear_validation(root)
    status, result = submit(bundle, data, wait=wait)
    code = _exit(status)
    if code == TRANSIENT:
        items = request.get("work_items") or []
        for item in items:
            result["park"] = _park(bundle, item, request.get("run"), "no final answer from POST /changesets")
        if items:
            result["reverted"] = _revert(root, request["files"])
        return code, result
    if code == OK:
        committed = {entry["path"]: _sha(entry["content"].encode("utf-8"))
                     for entry in request["files"] if entry["op"] == "put"}
        try:
            with _locked(root):  # onto the state as it is now: a pull may have run while the writer judged
                state = load(root)
                _write(root / META / "workspace.json", json.dumps(
                    {**state, "discard": {**(state.get("discard") or {}), **committed}}, indent=1).encode())
            result["workspace"] = pull(root, bundle)
        except (WorkspaceError, OSError) as exc:
            result["workspace"] = {"error": str(exc), "hint": "committed; run: ai-wiki workspace pull"}
            return OK, result
        if result["workspace"]["conflicts"]:
            code = CONFLICT
    return code, result


# --- concepts ---------------------------------------------------------------------------


def concept_new(root: Path, rel: str, *, type_: str, title: str, description: str, tags: list[str],
                source_id: str) -> Path:
    """Write a concept skeleton without service-owned keys; the agent writes the body."""
    load(root)
    rel = unicodedata.normalize("NFC", rel)
    error = changeset._path_error(rel)
    if error:
        raise WorkspaceError(f"{rel}: {error['message']}")
    if not changeset._EVIDENCE_ID.fullmatch(source_id):
        raise WorkspaceError("--source-id must be 1-80 letters, digits, '-' or '_'")
    if not tags or not all(value.strip() for value in (type_, title, description, *tags)):
        raise WorkspaceError("--type, --title, --description and --tags must not be empty")
    target = root / rel
    if target.exists() or target.is_symlink():
        raise WorkspaceError(f"{rel} already exists; edit it instead")
    frontmatter = {"type": type_, "title": title, "description": description, "tags": tags,
                   "sources": [{"id": source_id, "resource": changeset.PACKET_RESOURCE}]}
    text = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=4096) + "---\n# Summary\n\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# --- command output ---------------------------------------------------------------------


def _diff(before: str, after: str, rel: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        f"a/{rel}", f"b/{rel}"))


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace") if path.is_file() else ""


def _print(result: dict, as_json: bool, *, name: str, fields: tuple[str, ...]) -> None:
    result.pop("files", None)  # stamped bytes: diff --stamped shows them
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    errors = result.get("errors") or []
    emit(object_lines(name, {key: result.get(key) for key in fields if key in result}),
         table_lines("errors", errors, ("code", "path", "line", "message", "hint")) if errors else [],
         table_lines("warnings", result.get("warnings") or [], ("code", "path", "message"))
         if result.get("warnings") else [])


def run(a, bundle: str | None) -> int:
    """Dispatch a parsed workspace, concept, validate or propose command; ``bundle`` is ``-b``."""
    root = Path(a.dir).expanduser()
    if a.cmd == "workspace" and a.action == "pull":
        state_bundle = load(root)["bundle"] if (root / META / "workspace.json").exists() else None
        bundle = bundle or state_bundle or cli._active() or _default_bundle()
        result = pull(root, bundle)
        if a.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            emit(object_lines("workspace", {key: result[key] for key in ("bundle", "base_revision", "up_to_date")}),
                 object_lines("count", {key: len(result[key]) for key in ("updated", "kept", "conflicts")}),
                 table_lines("conflicts", ({"path": rel, "mine": rel + MINE} for rel in result["conflicts"]),
                             ("path", "mine")) if result["conflicts"] else [])
        return CONFLICT if result["conflicts"] else OK

    state = load(root)
    if bundle and bundle != state["bundle"]:
        raise WorkspaceError(f"{root} holds bundle {state['bundle']!r}, not {bundle!r}")
    if a.cmd == "concept":
        target = concept_new(root, a.path, type_=a.type, title=a.title, description=a.description,
                             tags=[tag.strip() for tag in a.tags.split(",")], source_id=a.source_id)
        emit(object_lines("concept", {"path": target.relative_to(root).as_posix(), "created": True}))
        return OK
    if a.cmd == "workspace" and a.action == "status":
        changed, _local = changes(root, state)
        rows = [{"path": rel, "change": change} for rel, change in changed.items()]
        pending = conflicts(root)
        if a.json:
            print(json.dumps({"bundle": state["bundle"], "base_revision": state["base_revision"],
                              "changes": rows, "conflicts": pending}, ensure_ascii=False, indent=2))
        else:
            emit(object_lines("workspace", {"bundle": state["bundle"], "base_revision": state["base_revision"],
                                            "clean": not rows and not pending}),
                 table_lines("changes", rows, ("path", "change")) if rows else [],
                 table_lines("conflicts", ({"path": rel, "mine": rel + MINE} for rel in pending),
                             ("path", "mine")) if pending else [])
        return OK

    base = root / META / "base"
    if a.cmd == "workspace" and not a.stamped:  # diff
        diffs = {rel: _diff(_read(base / rel), _read(root / rel), rel) for rel in changes(root, state)[0]}
        print(json.dumps(diffs, ensure_ascii=False, indent=2) if a.json else "".join(diffs.values()), end="")
        return OK
    with _locked(root):  # built and judged on one base: a pull waits, never half way through either
        state = load(root)
        request, evidence = build(root, state, **_changeset_options(a))
        if a.cmd == "workspace":  # diff --stamped
            result = judge(root, state, request, evidence)
            if result["status"] == "rejected":
                _print(result, a.json, name="validation", fields=("status", "http_status"))
                return verdict_code(result)
            diffs = {rel: _diff(_read(base / rel), text, rel) for rel, text in result["files"].items()}
            print(json.dumps(diffs, ensure_ascii=False, indent=2) if a.json else "".join(diffs.values()), end="")
            return OK
        if a.cmd == "validate":
            result = judge(root, state, request, evidence)
            _record_validation(root, state, request, evidence, result, cacheable=bool(a.item or a.upload))
            _print(result, a.json, name="validation",
                   fields=("status", "http_status", "validation", "changeset_sha256"))
            return verdict_code(result)
    code, result = propose(root, request, evidence, dry_run=a.dry_run, wait=a.wait)
    if not a.dry_run:
        from aiwiki.cli import maint

        maint.record(Path(a.state_dir).expanduser().resolve(), request, code, result)  # for the run's report
    _print(result, a.json, name="changeset", fields=(
        "id", "status", "http_status", "deduplicated", "noop", "commit", "dry_run", "closed_items", "error", "detail",
        "park", "workspace"))
    return code


def _changeset_options(a) -> dict:
    return {"item": a.item, "upload": Path(a.upload).expanduser() if a.upload else None, "source_id": a.source_id,
            "deprecate": a.deprecate or (), "retire": getattr(a, "retire", None) or (),
            "allow_shrink": a.allow_shrink or (),
            "allow_retype": a.allow_retype or (), "allow_grow": getattr(a, "allow_grow", None) or (), "run": a.run,
            "close": not getattr(a, "no_close", False)}


def _default_bundle() -> str:
    status, _headers, body = cli._http("GET", "/health")
    name = _json(body).get("bundle") if status == 200 else None
    if not name:
        raise WorkspaceError("no bundle selected: pass -b <bundle> or run ai-wiki bundle use <name>")
    return name
