"""The auditor's loop (design §3, §5): ``ai-wiki review …``.

    begin     doctor --role auditor, the auditor lease, a workspace of the published bundle
              (--as-human: doctor --role reviewer, for the owner's human-reviewed verdicts)
    next      the next concept the writer's backlog lists, with its content hash and evidence
              (--path: a human run takes any concept, backlog or not)
    evidence  re-check the concept's frozen evidence, re-read its Git parts from this host, and
              list related frozen sources it does not cite
    verdict   record verified, corrected (the workspace edit) or unverified, with a note
    submit    send the recorded verdicts as audit changesets of at most 5 reviews
    triage next | verdict | submit
              re-check the maintainer's skips and duplicates (service/triage.py): take the next
              one the writer lists with its frozen files, record upheld or missed (the claims it
              lost, quoting those files), send the verdicts
    end       release the lease and summarize the run

The writer derives the backlog and judges every verdict (§5.4): it stamps ``verified`` and
``generated``, restores what a review may not change, and downgrades a correction that adds
anything to unverified. Nothing here reads a curator's hand-off: only the concept, its cited
sources and what this host re-reads. A run keeps ``<state-dir>/reviews/<run>/review.json`` and
its workspace ``ws/`` and the triage items' frozen files under ``triage/<item>/``;
``reviews/current-<bundle>.json`` names the bundle's run.

Exit codes: 0 ok, 1 an unexpected failure, 2 usage, 4 preflight failed, 6 some reviews were
dropped (rejected or conflicting), 8 no final answer (resend with submit), 10 the backlog
holds nothing more, 11 the run's budget is spent.
"""
from __future__ import annotations

import argparse
import array
import collections
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat as stat_mod
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import yaml

from aiwiki.cli import main as cli
from aiwiki.cli import maint
from aiwiki.cli.maint import MaintError
from aiwiki.runtime import changeset
from aiwiki.runtime.changeset import VERDICTS

OK, FAILED, USAGE, PREFLIGHT, DROPPED, TRANSIENT, EMPTY, BUDGET = 0, 1, 2, 4, 6, 8, 10, 11
BATCH = 5  # reviews per audit changeset (design §2.3)
TRIAGE_MAX = 20  # triage items a run takes unless begin says otherwise
# After this, a run takes no more concepts or triage items: it submits and ends in time, inside Multica's
# 3 h task timeout. 2026-11-16: at 120 minutes, concepts had 90 minutes a run (33-45 reviewed, 0.4-0.55 a
# minute) and six runs a day reviewed fewer concepts than the maintainer wrote; the backlog went 269 -> 345.
DEADLINE_S = 150 * 60
# The deadline's last part is the triage review's: concepts stop taking this long before it. 2026-11-15:
# the first two runs with max 100 spent the whole deadline on concepts and re-checked no skip.
TRIAGE_RESERVE_S = 30 * 60
# Related evidence: the concept's distinctive identifiers (backticked names, file paths, camelCase,
# snake_case, UPPER_CASE and dotted names) found in frozen sources it does not cite. An audit that
# reads only the cited sources re-verified graph-jsx while a newer uncited source showed
# GraphController replacing GraphJsxController (audit report §2.3).
_IDENTIFIER = re.compile(
    r"`([^`\n]+)`"
    r"|(?<![\w/.-])((?:[\w.-]+/)+[\w.-]*\.[A-Za-z]{1,6})(?![\w/])"
    r"|(?<![\w.])([a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)(?!\w)"
    r"|(?<![\w.])([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?!\w)"
    r"|(?<![\w.])([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+)(?!\w)")
_LINK_TARGET = re.compile(r"\]\([^)]*\)")  # a concept link names a concept, not the system
_RELATED_WORD_B = re.compile(rb"[A-Za-z0-9_]+")
_RELATED_WORD_S = re.compile(r"[A-Za-z0-9_]+")
_RELATED_INDEX = "related-index.sqlite"
_RELATED_SCHEMA = f"2-{sys.byteorder}-{array.array('I').itemsize}"
_RELATED_RACY_NS = 2_000_000_000  # 2 s racy-clean guard, matching workspace._RACY_NS
_RELATED_MAX_STALE_IDS = 500
_RELATED_LOCK_WAIT_S = 60.0
RELATED, COMMON = 5, 30  # rows shown; an identifier in more sources than this distinguishes nothing


def _folder(state_dir: Path, run: str) -> Path:
    return state_dir / "reviews" / maint._slug(run)


def _pointer(state_dir: Path, bundle: str) -> Path:
    return state_dir / "reviews" / f"current-{maint._slug(bundle)}.json"


def _load(state_dir: Path, bundle: str | None) -> dict:
    """The current run's record: of ``-b``, else of the only bundle reviewed here."""
    if bundle:
        current = maint._read(_pointer(state_dir, bundle)) or {}
    else:
        currents = [c for path in sorted((state_dir / "reviews").glob("current-*.json")) if (c := maint._read(path))]
        if len(currents) > 1:
            raise MaintError("reviews of " + ", ".join(sorted(str(c.get("bundle")) for c in currents))
                             + " are active here; pass -b <bundle>", USAGE)
        current = currents[0] if currents else {}
    state = maint._read(_folder(state_dir, current["run"]) / "review.json") if current.get("run") else None
    if state is None:
        raise MaintError("no review run is active here; run: ai-wiki review begin --run <id>", USAGE)
    return state


def _save(state_dir: Path, state: dict) -> None:
    maint._write(_folder(state_dir, state["run"]) / "review.json", state)


def _backlog(bundle: str) -> dict:
    return maint._call("GET", "/audit/backlog", bundle=bundle, params={"limit": 1000})[1]


def begin(bundle: str, *, run: str, state_dir: Path, max_reviews: int, workspace_dir: Path | None,
          as_human: bool = False, triage_max: int = TRIAGE_MAX, deadline_s: int = DEADLINE_S,
          triage_reserve_s: int = TRIAGE_RESERVE_S) -> tuple[int, dict]:
    """doctor → auditor lease → workspace pull → the backlog's size. ``as_human`` preflights a
    human reviewer (design §5.5), whom the writer does not hold to the backlog. From the first begin,
    ``review next`` exits 11 after ``deadline_s - triage_reserve_s`` and ``triage next`` after ``deadline_s``."""
    from aiwiki.cli import doctor, update, workspace

    folder = _folder(state_dir, run)
    state = maint._read(folder / "review.json") or {  # a begin repeated within the run keeps its record
        "run": run, "bundle": bundle, "max": max_reviews, "taken": [], "pending": {}, "submitted": [], "dropped": []}
    if triage_reserve_s >= deadline_s:
        raise MaintError(f"--triage-reserve ({triage_reserve_s} s) must be shorter than --deadline ({deadline_s} s)",
                         USAGE)
    # A begin repeated within the run keeps the deadlines it has. A run 0.3.31 recorded has only the
    # deadline: its concepts keep that one too, never a later one computed from this begin.
    now = maint._now()
    state.setdefault("concept_deadline", state.get("deadline") or
                     maint._iso(now + timedelta(seconds=deadline_s - triage_reserve_s)))
    state.setdefault("deadline", maint._iso(now + timedelta(seconds=deadline_s)))
    if state["bundle"] != bundle:
        raise MaintError(f"run {run} reviews bundle {state['bundle']!r}, not {bundle!r}", USAGE)
    state["human"] = as_human
    _triage(state, triage_max)
    checked = doctor.run("reviewer" if as_human else "auditor", bundle=bundle, state_dir=state_dir, skills_dir=None)
    if not checked["ok"]:
        failed = [row for row in checked["checks"] if not row["ok"]]
        return PREFLIGHT, {"run": run, "failed": "doctor", "checks": failed}
    # Shared update.lock until the run pointer is written: `ai-wiki update` never installs under a beginning run.
    started = update.UpdateLock(shared=True)
    try:
        acquired = started.acquire()
    except update.StateUnwritable as exc:
        return PREFLIGHT, {"run": run, "failed": "state_unwritable", "error": f"{exc}; no lease was taken"}
    if not acquired:
        return PREFLIGHT, {"run": run, "failed": "update_running", "error": f"{started.path} stayed held for "
                           f"{update.LOCK_TIMEOUT} s by an `ai-wiki update`; no lease was taken"}
    try:
        try:
            lease = maint._call("POST", "/maint/lease/auditor", bundle=bundle, run=run)[1]["lease"]
        except MaintError as exc:
            return PREFLIGHT, {"run": run, "failed": "lease", "error": str(exc), "holder": exc.detail}
        state["workspace"] = str((workspace_dir or folder / "ws").expanduser().resolve())
        try:
            pulled = workspace.pull(Path(state["workspace"]), bundle)
            found = _backlog(bundle)
        except (MaintError, workspace.WorkspaceError, OSError) as exc:
            with contextlib.suppress(MaintError):
                maint._call("DELETE", "/maint/lease/auditor", bundle=bundle, run=run)
            return PREFLIGHT, {"run": run, "failed": "workspace", "error": str(exc)}
        state["mode"] = found.get("mode")  # codex: the writer still runs Codex audits, so this run shadows them
        try:
            triaged = _triage_backlog(bundle, 1)
        except MaintError:
            triaged = None  # the triage backlog never holds the concept reviews back
        _save(state_dir, state)
        maint._write(_pointer(state_dir, bundle), {"run": run, "bundle": bundle})
    finally:
        started.release()
    return OK, {"run": run, "bundle": bundle, "workspace": state["workspace"], "base_revision": pulled["base_revision"],
                "lease_expires_at": lease.get("expires_at"), "mode": found.get("mode"), "epoch": found.get("epoch"),
                "backlog": found.get("total"), "seed": found.get("seed"),
                "budget": {"max": state["max"], "taken": len(state["taken"]), "deadline": state["concept_deadline"]},
                "triage": {"backlog": triaged and triaged.get("total"), "epoch": triaged and triaged.get("epoch"),
                           "max": _triage(state)["max"], "taken": len(_triage(state)["taken"]),
                           "deadline": state["deadline"]},
                "next": f"ai-wiki -b {bundle} review next --json"}


def _chosen(root: Path, path: str) -> dict:
    """A concept a human names, as a backlog entry: its published content hash and cited sources."""
    from aiwiki.engine.document import parse_document
    from aiwiki.engine.scan_sources import _source_resource_rel

    concept = root / path
    if Path(path).is_absolute() or ".." in Path(path).parts or not concept.is_file() or concept.is_symlink():
        raise MaintError(f"{path} is not a concept of the published bundle", USAGE)
    text = concept.read_text(encoding="utf-8")
    sources = parse_document(text).frontmatter.get("sources")
    cited = {_source_resource_rel(source["resource"].strip(), path) for source in
             (sources if isinstance(sources, list) else []) if isinstance(source, dict)
             and isinstance(source.get("resource"), str)}
    return {"path": path, "base": changeset.content_hash(text), "reason": "human",
            "sources": sorted(rel for rel in cited if rel and rel.startswith("sources/") and (root / rel).is_file())}


def next_concept(state_dir: Path, bundle: str | None, path: str | None = None) -> tuple[int, dict]:
    """The first backlog concept this run has not taken, as the published workspace holds it;
    with ``path``, that concept (a human run only)."""
    from aiwiki.cli import workspace

    state = _load(state_dir, bundle)
    if path is not None and not state.get("human"):
        raise MaintError("--path takes a concept outside the backlog: only a run begun --as-human may", USAGE)
    spent = _spent(state, len(state["taken"]), state["max"], "concept_deadline")
    if spent:
        return BUDGET, {"stopped": spent, "taken": len(state["taken"]), "pending": len(state["pending"]),
                        "next": f"ai-wiki -b {state['bundle']} review submit" if state["pending"] else
                        f"ai-wiki -b {state['bundle']} review end --run {state['run']}"}
    root = Path(state["workspace"])
    if path is not None:
        workspace.pull(root, state["bundle"])  # the version the writer judges
        entry = _chosen(root, path)
    else:
        found = _backlog(state["bundle"])
        # A correction is another run's to review, from its reviewable_at (design §5.4 A4); the
        # writer judges by its own clock, so this only saves a review it would refuse.
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = next((row for row in found.get("concepts") or [] if row["path"] not in state["taken"]
                      and row.get("corrected_run") != state["run"] and (row.get("reviewable_at") or "") <= now),
                     None)
        if entry is None:
            return EMPTY, {"stopped": "backlog_empty", "pending": len(state["pending"]),
                           "next": f"ai-wiki -b {state['bundle']} review submit" if state["pending"] else
                           f"ai-wiki -b {state['bundle']} review end --run {state['run']}"}
        if workspace.load(root)["base_revision"] != found.get("revision"):
            workspace.pull(root, state["bundle"])  # verdicts keep their own copy: the workspace stays clean
    if entry["path"] not in state["taken"]:
        state["taken"].append(entry["path"])
    state.setdefault("entries", {})[entry["path"]] = {"base": entry["base"], "reason": entry["reason"]}
    _save(state_dir, state)
    path = entry["path"]
    return OK, {
        "path": path, "base": entry["base"], "reason": entry["reason"], "type": entry.get("type"),
        "title": entry.get("title"), "generated": entry.get("generated"),
        "concept": str(root / path), "evidence": [str(root / rel) for rel in entry.get("sources") or []],
        "budget": {"taken": len(state["taken"]), "max": state["max"], "pending": len(state["pending"]),
                   "deadline": state.get("concept_deadline") or state.get("deadline")},
        "commands": [f"ai-wiki -b {state['bundle']} review evidence {path}",
                     f"ai-wiki -b {state['bundle']} review verdict {path} verified|corrected|unverified "
                     "--note '<why>'"],
    }


def _parts(text: str) -> list[dict]:
    """The origin parts in a packet's header (design §5.6). The service writes a header only for
    several parts: a one-file packet keeps collected bytes, which may merely look like one."""
    if not text.startswith("---\nai_wiki_evidence:"):
        return []
    try:
        header = yaml.safe_load(text[4:text.index("\n---\n", 4)])
    except (ValueError, yaml.YAMLError):
        return []
    parts = header.get("parts") if isinstance(header, dict) else None
    parts = [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []
    return parts if len(parts) > 1 else []


def _reread(part: dict, repos: object) -> tuple[str, str, bytes | None]:
    """A git-file part read again from a reference checkout on this host: (status, detail, bytes)."""
    from aiwiki.maint import collect_repos

    if part.get("kind") != "git-file" or not all(part.get(key) for key in ("remote", "commit", "path")):
        return "unavailable", f"a {part.get('kind') or 'item'} part is not re-readable here", None
    if not isinstance(repos, dict) or not repos.get("root"):
        return "unavailable", "set repos.root in --config to re-read Git parts", None
    try:
        identity = collect_repos.canonical_remote(str(part["remote"]))
        for checkout in collect_repos.discover_repositories(Path(repos["root"]).expanduser())[0]:
            with contextlib.suppress(collect_repos.ScanError):
                if collect_repos.remote_identity(collect_repos.local_remote(checkout))[0] == identity \
                        and collect_repos.has_commit(checkout, str(part["commit"])):
                    blob = collect_repos.run_git("rev-parse", "--verify", f"{part['commit']}:{part['path']}",
                                                 cwd=checkout)
                    data = collect_repos.read_blob(checkout, blob)
                    if part.get("lines"):
                        first, last = part["lines"]
                        data = "".join(data.decode(errors="replace").splitlines(keepends=True)[first - 1:last]).encode()
                    return "reread", f"{checkout.name}@{str(part['commit'])[:7]}:{part['path']}", data
    except (ValueError, TypeError, collect_repos.ScanError) as exc:
        return "unavailable", str(exc)[:200], None
    return "unavailable", "no checkout under repos.root holds that commit", None


def _identifiers(document) -> set[str]:
    """The distinctive names the concept's body, title and aliases state."""
    aliases = document.frontmatter.get("aliases")
    text = "\n".join([document.body, str(document.frontmatter.get("title") or ""),
                      *(str(alias) for alias in (aliases if isinstance(aliases, list) else []))])
    found = set()
    for match in _IDENTIFIER.finditer(_LINK_TARGET.sub(" ", text)):
        value = next(group for group in match.groups() if group).strip()
        if len(value) >= 4 and re.search(r"[A-Za-z]", value):
            found.add(value)
    return found


def _related_direct(
    root: Path,
    document,
    frozen: dict,
    cited: set[str],
    *,
    identifiers: set[str] | None = None,
) -> list[dict]:
    """Direct full-tree scan reference implementation for ``_related`` (also used as fallback)."""
    identifiers = _identifiers(document) if identifiers is None else identifiers
    if not identifiers:
        return []
    holds: dict[str, set[str]] = {}
    for rel, recorded in frozen.items():
        rel_s = str(rel)
        file = root / rel_s
        if not rel_s.startswith("sources/") or "inbox" in Path(rel_s).parts:
            continue
        try:
            st = os.lstat(file)
        except OSError:
            continue
        if not stat_mod.S_ISREG(st.st_mode):
            continue
        data = file.read_bytes()
        if hashlib.sha256(data).hexdigest() != recorded:
            continue  # not the bytes the service froze: no evidence
        text = data.decode("utf-8", errors="replace")
        holds[rel_s] = {name for name in identifiers if name in text and re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text)}
    stated = {name: sum(name in found for found in holds.values()) for name in identifiers}
    rows = [{"source": rel, "matches": len(names), "identifiers": sorted(names)}
            for rel, found in holds.items() if rel not in cited
            and len(names := {name for name in found if stated[name] <= COMMON}) >= 2]
    return sorted(rows, key=lambda row: (-row["matches"], row["source"]))[:RELATED]


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


_NULL_STAT = (None, None, None, None, None)


def _unlink_sqlite(index_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{index_path}{suffix}")
        with contextlib.suppress(OSError):
            if p.is_file() or p.is_symlink():
                p.unlink()


@contextlib.contextmanager
def _related_locked(index_path: Path):
    """Exclusive flock on ``<index_path>.lock`` serializing schema init, sync and query across
    concurrent commands on the same review run (matching ``workspace._locked``)."""
    if index_path.is_dir():
        raise OSError(f"{index_path} is a directory")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{index_path}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _RELATED_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{lock_path} stayed locked for {_RELATED_LOCK_WAIT_S}s") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _is_corrupt_error(exc: Exception) -> bool:
    if isinstance(exc, (ValueError, TypeError, OverflowError)):
        return True
    if isinstance(exc, sqlite3.DatabaseError):
        msg = str(exc).lower()
        return any(k in msg for k in ("not a database", "malformed", "corrupt", "no such table", "no such column"))
    return False


def _create_related_tables(con: sqlite3.Connection) -> None:
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DROP TABLE IF EXISTS meta")
        con.execute("DROP TABLE IF EXISTS files")
        con.execute("DROP TABLE IF EXISTS tokens")
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID")
        con.execute(
            "CREATE TABLE files (rel TEXT PRIMARY KEY, id INTEGER NOT NULL, "
            "dev INTEGER, ino INTEGER, size INTEGER, mtime_ns INTEGER, ctime_ns INTEGER, "
            "sha256 TEXT NOT NULL, indexed INTEGER NOT NULL) WITHOUT ROWID"
        )
        con.execute("CREATE TABLE tokens (token TEXT PRIMARY KEY, doc_count INTEGER, doc_ids BLOB) WITHOUT ROWID")
        con.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            (("schema", _RELATED_SCHEMA), ("max_fid", "0"), ("stale_ids", "0")),
        )
        con.execute("COMMIT")
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise


def _open_related_index(index_path: Path) -> sqlite3.Connection:
    if index_path.is_dir():
        raise OSError(f"{index_path} is a directory")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        con = sqlite3.connect(str(index_path), timeout=30.0, isolation_level=None)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            row = (
                con.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
                if {"meta", "files", "tokens"}.issubset(tables)
                else None
            )
            if row is None or row[0] != _RELATED_SCHEMA:
                _create_related_tables(con)
            return con
        except sqlite3.DatabaseError as exc:
            con.close()
            if attempt == 0 and _is_corrupt_error(exc):
                _unlink_sqlite(index_path)
                continue
            raise
    raise sqlite3.DatabaseError("failed to initialize related index")


def _sync_related_index(con: sqlite3.Connection, root: Path, frozen: dict) -> tuple[dict[int, str], set[int]]:
    now_ns = time.time_ns()
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    max_fid = int(meta.get("max_fid", "0"))
    stale_ids = int(meta.get("stale_ids", "0"))
    cached: dict[str, tuple[int, tuple, str, int]] = {
        rel: (fid, (dev, ino, size, mtime_ns, ctime_ns), sha, indexed)
        for rel, fid, dev, ino, size, mtime_ns, ctime_ns, sha, indexed in con.execute(
            "SELECT rel, id, dev, ino, size, mtime_ns, ctime_ns, sha256, indexed FROM files"
        )
    }

    eligible: dict[str, object] = {}
    for rel, recorded in frozen.items():
        rel_s = str(rel)
        if rel_s.startswith("sources/") and "inbox" not in Path(rel_s).parts:
            eligible[rel_s] = recorded

    id_to_rel: dict[int, str] = {}
    valid_ids: set[int] = set()
    delta_postings: dict[str, array.array] = collections.defaultdict(lambda: array.array("I"))
    upserts: list[tuple] = []
    deletes: list[str] = []

    for rel_s in sorted(eligible):
        recorded = eligible[rel_s]
        file = root / rel_s
        try:
            st = os.lstat(file)
        except OSError:
            st = None
        cur = cached.get(rel_s)
        if st is None or not stat_mod.S_ISREG(st.st_mode):
            if cur is not None:
                deletes.append(rel_s)
                if cur[3] == 1:
                    stale_ids += 1
            continue

        key = _stat_key(st)
        cacheable = (
            (now_ns - st.st_mtime_ns) >= _RELATED_RACY_NS
            and (now_ns - st.st_ctime_ns) >= _RELATED_RACY_NS
        )
        if cur is not None and cacheable and cur[1] == key:
            if cur[2] == recorded:
                if cur[3] == 1:
                    id_to_rel[cur[0]] = rel_s
                    valid_ids.add(cur[0])
                    continue
                # Previously drifted, now ledger matches: fall through to tokenize once.
            else:
                continue

        data = file.read_bytes()
        stored = _NULL_STAT
        if cacheable:
            try:
                if _stat_key(os.lstat(file)) == key:
                    stored = key
            except OSError:
                stored = _NULL_STAT

        sha = hashlib.sha256(data).hexdigest()
        if cur is not None and cur[2] == sha:
            fid, indexed = cur[0], cur[3]
            if sha == recorded and indexed == 0:
                for tok in set(_RELATED_WORD_B.findall(data)):
                    delta_postings[tok.decode("ascii")].append(fid)
                indexed = 1
            if cur[1] != stored or cur[3] != indexed:
                upserts.append((rel_s, fid, *stored, sha, indexed))
            if sha == recorded:
                id_to_rel[fid] = rel_s
                valid_ids.add(fid)
        else:
            if cur is not None and cur[3] == 1:
                stale_ids += 1
            max_fid += 1
            fid = max_fid
            indexed = 0
            if sha == recorded:
                for tok in set(_RELATED_WORD_B.findall(data)):
                    delta_postings[tok.decode("ascii")].append(fid)
                indexed = 1
                id_to_rel[fid] = rel_s
                valid_ids.add(fid)
            upserts.append((rel_s, fid, *stored, sha, indexed))

    for rel_s, cur in cached.items():
        if rel_s not in eligible:
            deletes.append(rel_s)
            if cur[3] == 1:
                stale_ids += 1

    if stale_ids > _RELATED_MAX_STALE_IDS:
        _create_related_tables(con)
        return _sync_related_index(con, root, frozen)

    if deletes or upserts or delta_postings:
        con.execute("BEGIN IMMEDIATE")
        try:
            if deletes:
                con.executemany("DELETE FROM files WHERE rel = ?", ((r,) for r in deletes))
            if upserts:
                con.executemany(
                    "INSERT INTO files (rel, id, dev, ino, size, mtime_ns, ctime_ns, sha256, indexed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(rel) DO UPDATE SET id=excluded.id, dev=excluded.dev, ino=excluded.ino, "
                    "size=excluded.size, mtime_ns=excluded.mtime_ns, ctime_ns=excluded.ctime_ns, "
                    "sha256=excluded.sha256, indexed=excluded.indexed",
                    upserts,
                )
            if delta_postings:
                existing: dict[str, array.array] = {}
                tok_keys = list(delta_postings)
                for off in range(0, len(tok_keys), 500):
                    chunk = tok_keys[off:off + 500]
                    q = ",".join("?" * len(chunk))
                    for tok, blob in con.execute(f"SELECT token, doc_ids FROM tokens WHERE token IN ({q})", chunk):
                        if not isinstance(blob, (bytes, bytearray)):
                            raise TypeError("corrupt tokens.doc_ids payload")
                        arr = array.array("I")
                        arr.frombytes(blob)
                        existing[tok] = arr
                tok_upserts = []
                for tok, add_arr in delta_postings.items():
                    prev = existing.get(tok)
                    if prev is None:
                        merged = add_arr
                    else:
                        prev.extend(add_arr)
                        merged = prev
                    tok_upserts.append((tok, len(merged), merged.tobytes()))
                con.executemany(
                    "INSERT INTO tokens (token, doc_count, doc_ids) VALUES (?, ?, ?) "
                    "ON CONFLICT(token) DO UPDATE SET doc_count=excluded.doc_count, doc_ids=excluded.doc_ids",
                    tok_upserts,
                )
            con.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (("max_fid", str(max_fid)), ("stale_ids", str(stale_ids))),
            )
            con.execute("COMMIT")
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise

    return id_to_rel, valid_ids


def _related_query(
    con: sqlite3.Connection,
    root: Path,
    identifiers: set[str],
    cited: set[str],
    id_to_rel: dict[int, str],
    valid_ids: set[int],
) -> list[dict]:
    if not identifiers:
        return []
    needed_toks: set[str] = set()
    id_parts: dict[str, list[str]] = {}
    for name in identifiers:
        parts = _RELATED_WORD_S.findall(name)
        if parts:
            id_parts[name] = parts
            needed_toks.update(parts)

    tok_map: dict[str, tuple[int, array.array]] = {}
    tok_list = list(needed_toks)
    for off in range(0, len(tok_list), 500):
        chunk = tok_list[off:off + 500]
        q = ",".join("?" * len(chunk))
        for tok, cnt, blob in con.execute(
            f"SELECT token, doc_count, doc_ids FROM tokens WHERE token IN ({q})", chunk
        ):
            if not isinstance(blob, (bytes, bytearray)):
                raise TypeError("corrupt tokens.doc_ids payload")
            arr = array.array("I")
            arr.frombytes(blob)
            tok_map[tok] = (cnt, arr)

    holds: dict[str, set[str]] = collections.defaultdict(set)
    bytes_cache: dict[str, bytes] = {}
    for name, parts in id_parts.items():
        if len(parts) == 1 and parts[0] == name:
            entry = tok_map.get(name)
            if not entry:
                continue
            valid_docs = {doc_id for doc_id in entry[1] if doc_id in valid_ids}
            if len(valid_docs) > COMMON:
                continue  # exact early-out: stated[name] > COMMON is always dropped
            for doc_id in valid_docs:
                holds[id_to_rel[doc_id]].add(name)
        else:
            uniq = set(parts)
            if any(tok not in tok_map for tok in uniq):
                continue
            cands: set[int] | None = None
            for tok in sorted(uniq, key=lambda t: tok_map[t][0]):
                docs = {doc_id for doc_id in tok_map[tok][1] if doc_id in valid_ids}
                cands = docs if cands is None else (cands & docs)
                if not cands:
                    break
            if not cands:
                continue
            use_str = "\ufffd" in name
            if use_str:
                spat = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
            else:
                bname = name.encode("utf-8")
                bpat = re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(bname) + rb"(?![A-Za-z0-9_])")
            matched: list[str] = []
            too_common = False
            for doc_id in cands:
                rel = id_to_rel[doc_id]
                raw = bytes_cache.get(rel)
                if raw is None:
                    raw = (root / rel).read_bytes()
                    bytes_cache[rel] = raw
                if use_str:
                    text = raw.decode("utf-8", errors="replace")
                    hit = name in text and bool(spat.search(text))
                else:
                    hit = bname in raw and bool(bpat.search(raw))
                if hit:
                    matched.append(rel)
                    if len(matched) > COMMON:
                        too_common = True
                        break
            if not too_common:
                for rel in matched:
                    holds[rel].add(name)

    rows = [{"source": rel, "matches": len(names), "identifiers": sorted(names)}
            for rel, names in holds.items() if rel not in cited and len(names) >= 2]
    return sorted(rows, key=lambda row: (-row["matches"], row["source"]))[:RELATED]


def _related(
    root: Path,
    document,
    frozen: dict,
    cited: set[str],
    *,
    index_path: Path | None = None,
) -> list[dict]:
    """Up to ``RELATED`` frozen sources of the workspace (``sources/`` but its inbox) that the
    concept does not cite and that state at least two of its identifiers, most first. An
    identifier that more than ``COMMON`` sources state is dropped. Client-side only: the writer
    keeps no index, so this reads the workspace the run pulled (using a compact run-scoped SQLite
    inverted index when ``index_path`` is given, falling back cleanly to a direct scan)."""
    identifiers = _identifiers(document)
    if not identifiers:
        return []
    if index_path is None:
        return _related_direct(root, document, frozen, cited, identifiers=identifiers)
    con: sqlite3.Connection | None = None
    try:
        with _related_locked(index_path):
            try:
                con = _open_related_index(index_path)
                id_to_rel, valid_ids = _sync_related_index(con, root, frozen)
                return _related_query(con, root, identifiers, cited, id_to_rel, valid_ids)
            except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
                if con is not None:
                    with contextlib.suppress(sqlite3.Error):
                        con.close()
                    con = None
                if _is_corrupt_error(exc):
                    _unlink_sqlite(index_path)
                return _related_direct(root, document, frozen, cited, identifiers=identifiers)
    except OSError:
        return _related_direct(root, document, frozen, cited, identifiers=identifiers)
    finally:
        if con is not None:
            with contextlib.suppress(sqlite3.Error):
                con.close()


def evidence(state_dir: Path, bundle: str | None, path: str, config: Path) -> tuple[int, dict]:
    """Re-check the concept's cited sources: the frozen bytes against ``sources/.hashes.yaml``,
    and each Git part of a packet re-read from this host's checkouts against its recorded sha256.
    A re-read copy lands in the run's ``evidence/``. ``related`` lists frozen sources the concept
    does not cite that state its identifiers: newer evidence may contradict it. The findings are
    for the note only: the writer never trusts a reviewer's word about evidence (design §5.4)."""
    from aiwiki.engine.document import load_yaml, parse_document
    from aiwiki.engine.scan_sources import _source_resource_rel

    state = _load(state_dir, bundle)
    root = Path(state["workspace"])
    concept = root / path
    if path not in state["taken"] or not concept.is_file():
        raise MaintError(f"{path} is not a concept this run took; run ai-wiki review next", USAGE)
    ledger = root / "sources" / ".hashes.yaml"  # {sources/<name>: sha256}, written by the service
    frozen = load_yaml(ledger.read_text(encoding="utf-8")) if ledger.is_file() else {}
    frozen = frozen if isinstance(frozen, dict) else {}
    settings = maint._config(config)
    rows, out, cited = [], _folder(state_dir, state["run"]) / "evidence", set()
    document = parse_document(concept.read_text(encoding="utf-8"))
    sources = document.frontmatter.get("sources") or []
    for source in sources if isinstance(sources, list) else []:
        resource = source.get("resource") if isinstance(source, dict) else None
        rel = _source_resource_rel(resource.strip(), path) if isinstance(resource, str) else None
        cited.add(rel)
        if not rel or not rel.startswith("sources/"):
            rows.append({"source": resource, "part": None, "status": "external", "detail": "not frozen evidence"})
            continue
        file = root / rel
        if not file.is_file():
            rows.append({"source": rel, "part": None, "status": "missing", "detail": "not in the published bundle"})
            continue
        data = file.read_bytes()
        digest, recorded = hashlib.sha256(data).hexdigest(), frozen.get(rel)
        rows.append({"source": rel, "part": None,
                     "status": "frozen" if recorded == digest else "unrecorded" if recorded is None else "drifted",
                     "detail": f"sha256 {digest[:12]}" + (f", recorded {str(recorded)[:12]}" if recorded != digest
                                                          and recorded is not None else "")})
        text = data.decode("utf-8", errors="replace")
        # Header values are collected data: they pick what to re-read, never where a copy lands.
        for index, part in enumerate(_parts(text) if recorded == digest else [], start=1):
            status, detail, reread = _reread(part, settings.get("repos"))
            if reread is not None:
                copy = out / f"{Path(rel).name}.part{index}"
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(reread)
                # A match needs the Git text itself in the packet, not only the sha256 its header claims.
                same = hashlib.sha256(reread).hexdigest() == part.get("sha256") \
                    and reread.decode("utf-8", errors="replace").rstrip("\n") in text
                clipped = part.get("truncated") or part.get("redactions")
                status = "match" if same else "differs (truncated or redacted)" if clipped else "differs"
                detail = f"{detail}; re-read copy {copy}"
            rows.append({"source": rel, "part": part.get("ref"), "status": status, "detail": detail})
    index_path = _folder(state_dir, state["run"]) / _RELATED_INDEX
    related = _related(root, document, frozen, cited, index_path=index_path)
    return OK, {"path": path, "evidence": rows, "related": related}


def verdict(state_dir: Path, bundle: str | None, path: str, value: str, note: str) -> tuple[int, dict]:
    """Record a verdict. ``corrected`` takes the workspace edit and puts the file back, so the
    workspace stays the published tree; ``verified`` and ``unverified`` refuse an edit."""
    from aiwiki.cli import workspace

    state = _load(state_dir, bundle)
    root = Path(state["workspace"])
    entry = (state.get("entries") or {}).get(path)
    if entry is None:
        raise MaintError(f"{path} is not a concept this run took; run ai-wiki review next", USAGE)
    base_file = root / workspace.META / "base" / path
    current, base = (root / path).read_bytes(), base_file.read_bytes() if base_file.is_file() else b""
    record = {"base": entry["base"], "verdict": value, "note": note}
    if value == "corrected":
        if current == base:
            raise MaintError(f"{path} is unchanged: edit it in the workspace to correct it, or pass verified or "
                             "unverified", USAGE)
        record["content"] = current.decode("utf-8")
        (root / path).write_bytes(base)
    elif current != base:
        raise MaintError(f"{path} holds an edit: pass corrected to propose it, or restore the file", USAGE)
    state["pending"][path] = record
    _save(state_dir, state)
    return OK, {"path": path, "verdict": value, "pending": len(state["pending"]),
                "next": f"ai-wiki -b {state['bundle']} review submit" if len(state["pending"]) >= BATCH else
                f"ai-wiki -b {state['bundle']} review next --json"}


def submit(state_dir: Path, bundle: str | None, *, dry_run: bool, wait: float) -> tuple[int, dict]:
    """Send the pending verdicts in audit changesets of at most 5. A review the writer rejects or
    finds stale is dropped (its code kept) and the rest are sent again; the writer's receipt
    records the rest. A dry-run asks the writer's verdict, changes nothing and records it under
    ``dry_run`` (verdicts stay pending). While the writer runs Codex audits (mode codex) every
    submit is such a dry-run and settles its verdicts: a shadow run (design §9, phase 4a)."""
    from aiwiki.cli import workspace

    state = _load(state_dir, bundle)
    shadow = state.get("mode") == "codex"
    dry_run = dry_run or shadow
    root = Path(state["workspace"])
    results, dropped, code = [], [], OK
    pending = dict(state["pending"])
    while pending:
        batch = dict(list(pending.items())[:BATCH])
        request = {"schema": changeset.SCHEMA, "kind": "audit", "base_revision": workspace.load(root)["base_revision"],
                   "run": state["run"], "reviews": [{"path": path, **review} for path, review in batch.items()]}
        status, answer = workspace.submit(state["bundle"], json.dumps(request, ensure_ascii=False).encode(),
                                          dry_run=dry_run, wait=wait)
        refused = {error.get("path"): error.get("code") for error in answer.get("errors") or []
                   if isinstance(error, dict)}
        if status is None or status >= 500:
            code = TRANSIENT
            break
        if status in (401, 403):
            raise MaintError(f"the writer refused the review ({status}): {answer.get('detail')}", PREFLIGHT)
        if status in (409, 422) and refused.keys() & batch.keys():
            for path in refused.keys() & batch.keys():
                dropped.append({"path": path, "code": refused[path]})
                pending.pop(path)
            code = DROPPED
            continue
        if status not in (200, 201):
            raise MaintError(f"the writer refused the review ({status}): "
                             + "; ".join(f"{error.get('code')}: {error.get('message')}"
                                         for error in answer.get("errors") or []) or str(answer.get("detail")), FAILED)
        results += [{"job": answer.get("id"), **{key: row.get(key) for key in
                                                 ("path", "base", "verdict", "outcome", "downgrade")}}
                    for row in answer.get("reviews") or []]
        for path in batch:
            pending.pop(path)
    if dry_run:
        state.setdefault("dry_run", {}).update((row["path"], row) for row in results)
    if shadow or not dry_run:
        state["pending"] = pending
        state["dropped"] += dropped
    if not dry_run:
        state["submitted"] += results
    _save(state_dir, state)
    if results and not dry_run:
        with contextlib.suppress(workspace.WorkspaceError, OSError):
            workspace.pull(root, state["bundle"])
    return code, {"dry_run": dry_run, "shadow": shadow, "reviews": results, "dropped": dropped,
                  "pending": len(state["pending"])}


# --- triage review: the maintainer's skips and duplicates (service/triage.py) -----------------------


def _spent(state: dict, taken: int, limit: int, key: str = "deadline") -> str | None:
    """Why the run may take nothing more: ``budget`` (its count) or ``deadline`` (its time), else None.
    Concepts use ``concept_deadline`` (the triage reserve before the deadline), triage the deadline;
    a run recorded before the reserve has only the deadline, for both."""
    if taken >= limit:
        return "budget"
    deadline = state.get(key) or state.get("deadline")
    return "deadline" if deadline and maint._iso(maint._now()) >= deadline else None


def _triage(state: dict, max_items: int = TRIAGE_MAX) -> dict:
    """The run's triage record, made with ``max_items`` the first time (as ``max``, a begin
    repeated within the run keeps it); a run begun by an older CLI gets the default budget."""
    record = state.setdefault("triage", {"max": max_items, "taken": [], "entries": {}, "pending": {},
                                         "submitted": [], "dropped": [], "pulled": False})
    record.setdefault("unfetched", [])  # a run recorded by the first 0.3.6 build has none
    return record


def _triage_backlog(bundle: str, limit: int = 1000) -> dict | None:
    """The writer's triage backlog; None from a writer without the route (older than 0.3.6)."""
    try:
        return maint._call("GET", "/audit/backlog/triage", bundle=bundle, params={"limit": limit})[1]
    except MaintError as exc:
        if exc.status != 404:
            raise
        return None


def _pull_triage(state_dir: Path, run: str, bundle: str, entry: dict) -> tuple[list[dict] | None, str | None]:
    """The entry's frozen evidence files, written to ``triage/<item>/`` and checked by sha256, or
    None and why one could not be fetched intact."""
    item = entry["item"]
    folder, files = _folder(state_dir, run) / "triage" / item, []
    for file in entry["files"]:
        if not changeset._ITEM_FILE.fullmatch(f"{item}/{file['name']}"):
            return None, f"unusable file name {file['name']!r}"
        status, _headers, data = cli._http("GET", f"/maint/items/{item}/files/{quote(file['name'], safe='')}",
                                           bundle=bundle)
        if status != 200 or hashlib.sha256(data).hexdigest() != file["sha256"]:
            return None, f"{file['name']} could not be fetched intact ({status})"
        (folder / file["name"]).parent.mkdir(parents=True, exist_ok=True)
        (folder / file["name"]).write_bytes(data)
        files.append({"name": file["name"], "bytes": file["bytes"], "path": str(folder / file["name"])})
    return files, None


def triage_next(state_dir: Path, bundle: str | None) -> tuple[int, dict]:
    """The first triage item this run has not taken, its frozen evidence files fetched into
    ``triage/<item>/`` (checked by sha256), and the concept a duplicate_of names in the workspace.
    An item whose files cannot be fetched intact (a damaged blob) is passed over for this run
    (``unfetched``, reported by ``review end``), so it never holds the rest of the backlog back."""
    from aiwiki.cli import workspace

    state = _load(state_dir, bundle)
    record = _triage(state)
    tail = f"ai-wiki -b {state['bundle']} review triage submit" if record["pending"] else \
        f"ai-wiki -b {state['bundle']} review end --run {state['run']}"
    spent = _spent(state, len(record["taken"]), record["max"])
    if spent:
        return BUDGET, {"stopped": spent, "taken": len(record["taken"]), "pending": len(record["pending"]),
                        "next": tail}
    passed: list[dict] = []
    while True:
        seen = {*record["taken"], *(row["item"] for row in record["unfetched"])}
        # Oldest first: the first entry this run has not seen is among the first len(seen) + 1.
        found = _triage_backlog(state["bundle"], min(len(seen) + 1, 1000)) or {}
        entry = next((row for row in found.get("items") or [] if row["item"] not in seen), None)
        if entry is None:
            _save(state_dir, state)
            return EMPTY, {"stopped": "backlog_empty", "epoch": found.get("epoch"),
                           "pending": len(record["pending"]), "next": tail, **({"unfetched": passed} if passed else {})}
        item = entry["item"]
        if not changeset._WORK_ITEM.fullmatch(item):  # it names a folder here
            raise MaintError(f"the triage backlog names an unusable item {item!r}")
        files, why = _pull_triage(state_dir, state["run"], state["bundle"], entry)
        if files is not None:
            break
        passed.append({"item": item, "error": why})
        record["unfetched"].append(passed[-1])
    folder = _folder(state_dir, state["run"]) / "triage" / item
    root = Path(state["workspace"])
    if entry.get("duplicate_of") and not record.get("pulled"):
        # The concept a duplicate_of names, as published: one pull for the triage part of the run
        # (the writer archives the bundle for every pull, 304 or not).
        workspace.pull(root, state["bundle"])
        record["pulled"] = True
    target = entry.get("duplicate_of")  # the maintainer wrote it: only a concept path names a workspace file
    target = target if isinstance(target, str) and changeset._path_error(target) is None else None
    record["taken"].append(item)
    record["entries"][item] = {"files": {file["name"]: file["path"] for file in files}}
    _save(state_dir, state)
    return OK, {
        "item": item, "topic_key": entry["topic_key"], "origin": entry["origin"], "brief": entry.get("brief"),
        "status": entry["status"], "reason": entry["reason"], "note": entry.get("note"),
        "resolved_at": entry.get("resolved_at"), "dir": str(folder), "files": files,
        "duplicate_of": entry.get("duplicate_of") and {
            "path": entry["duplicate_of"], "concept": target and str(root / target),
            "exists": bool(target) and (root / target).is_file()},
        "budget": {"taken": len(record["taken"]), "max": record["max"], "pending": len(record["pending"])},
        **({"unfetched": passed} if passed else {}),
        "commands": [f"ai-wiki -b {state['bundle']} review triage verdict {item} upheld --note '<why>'",
                     f"ai-wiki -b {state['bundle']} review triage verdict {item} missed --note '<why>' "
                     "--missed-file <claims.json>"],
    }


def triage_verdict(state_dir: Path, bundle: str | None, item: str, value: str, note: str,
                   missed_file: Path | None) -> tuple[int, dict]:
    """Record upheld, or missed with the claims of ``missed_file`` (a JSON list of {claim,
    evidence: {file, lines: [first, last]}, target}), checked as the writer checks them."""
    from aiwiki.service import triage

    state = _load(state_dir, bundle)
    record = _triage(state)
    entry = record["entries"].get(item)
    if entry is None:
        raise MaintError(f"{item} is not a triage item this run took; run ai-wiki review triage next", USAGE)
    if (value == "missed") != (missed_file is not None):
        raise MaintError("missed takes --missed-file <claims.json>; upheld takes none", USAGE)
    try:
        missed = json.loads(missed_file.read_text(encoding="utf-8")) if missed_file else []
    except (OSError, ValueError) as exc:
        raise MaintError(f"--missed-file: {exc}", USAGE) from None
    review, errors = triage._shape({"item": item, "verdict": value, "missed": missed, "note": note})
    for index, claim in enumerate(review["missed"] if not errors else [], 1):
        path = entry["files"].get(claim["evidence"]["file"])
        found = triage.lines(Path(path).read_bytes()) if path else None
        if found is None or claim["evidence"]["lines"][1] > len(found):
            errors.append({"code": "evidence", "message": f"claim {index}: {claim['evidence']['file']} is not a text "
                           f"file of {item} with lines {claim['evidence']['lines']}" + (
                               f" (it has {len(found)})" if found is not None else "")})
    if errors:
        raise MaintError("; ".join(f"{error['code']}: {error['message']}" for error in errors), USAGE)
    record["pending"][item] = {key: review[key] for key in ("verdict", "missed", "note")}
    _save(state_dir, state)
    return OK, {"item": item, "verdict": value, "missed": len(review["missed"]), "pending": len(record["pending"]),
                "next": f"ai-wiki -b {state['bundle']} review triage next --json"}


def triage_submit(state_dir: Path, bundle: str | None) -> tuple[int, dict]:
    """Send the pending triage verdicts, at most 10 a request. The writer refuses a request
    whole; a review it names (not_due, self_review, evidence, secret_detected…) is dropped with
    its code and the rest are sent again. A resend after a lost answer returns the receipts."""
    from aiwiki.service import triage

    state = _load(state_dir, bundle)
    record = _triage(state)
    results, dropped, code, refusal = [], [], OK, None
    pending = dict(record["pending"])
    while pending:
        batch = dict(list(pending.items())[:triage.MAX_REVIEWS])
        try:
            status, answer = maint._call("POST", "/audit/backlog/triage", bundle=state["bundle"], run=state["run"],
                                         body={"reviews": [{"item": item, **review} for item, review in batch.items()]},
                                         ok=(200, 400, 409, 413, 422, 429))
        except MaintError as exc:
            # Unreachable or 5xx: a later submit resends the same reviews and gets their receipts.
            code, refusal = (TRANSIENT, None) if exc.status is None or exc.status >= 500 else (exc.code, exc)
            break
        if status == 200:
            results += [{key: row.get(key) for key in ("id", "item", "verdict", "status", "missed")}
                        for row in answer.get("reviews") or []]
            for item in batch:
                pending.pop(item)
            continue
        detail = answer.get("detail")  # a refusal before the reviews were read: {code, message}
        if isinstance(detail, dict) and detail.get("code") == "triage_off":
            # The operator turned it off (AIWIKI_TRIAGE_EPOCH unset): nothing pending can land.
            dropped += [{"item": item, "code": "triage_off"} for item in pending]
            pending, code = {}, DROPPED
            break
        errors = [error for error in answer.get("errors") or [] if isinstance(error, dict)] or (
            [detail] if isinstance(detail, dict) else [{"code": "refused", "message": str(detail)}])
        refused = {error.get("item"): error.get("code") for error in errors}
        if not refused.keys() & batch.keys():  # lease_required or a malformed request: nothing to drop
            refusal = MaintError(f"the writer refused the triage reviews ({status}): " + "; ".join(
                f"{error.get('code')}: {error.get('message')}" for error in errors), FAILED, status=status,
                detail=errors)
            break
        for item in refused.keys() & batch.keys():
            dropped.append({"item": item, "code": refused[item]})
            pending.pop(item)
        code = DROPPED
    record.update(pending=pending)
    record["dropped"] += dropped
    record["submitted"] += results
    _save(state_dir, state)  # what landed before a refusal stays recorded
    if refusal is not None:
        raise refusal
    return code, {"reviews": results, "dropped": dropped, "pending": len(pending)}


def end(state_dir: Path, bundle: str | None, run: str) -> tuple[int, dict]:
    """Release the auditor lease and summarize the run from its recorded receipts."""
    state = maint._read(_folder(state_dir, run) / "review.json")
    if state is None:
        raise MaintError(f"run {run} has not begun here", USAGE)
    if bundle and bundle != state["bundle"]:
        raise MaintError(f"run {run} reviews bundle {state['bundle']!r}, not {bundle!r}", USAGE)
    try:
        released = maint._call("DELETE", "/maint/lease/auditor", bundle=state["bundle"], run=run)[1]
    except MaintError as exc:
        released = {"released": False, "error": str(exc)}
    try:
        remaining = _backlog(state["bundle"]).get("total")
    except MaintError:
        remaining = None
    outcomes = [row.get("outcome") for row in state["submitted"]]
    record = _triage(state)
    try:
        triaged = _triage_backlog(state["bundle"], 1)
    except MaintError:
        triaged = None
    # missed counts the reviews that reopened their item; void ones judged nothing (service/triage.py).
    verdicts = [(row.get("verdict"), row.get("status")) for row in record["submitted"]]
    if (maint._read(_pointer(state_dir, state["bundle"])) or {}).get("run") == run:
        _pointer(state_dir, state["bundle"]).unlink()
    try:  # disk housekeeping never fails the run's summary
        _unlink_sqlite(_folder(state_dir, run) / _RELATED_INDEX)
        pruned = maint.prune_workspaces(state_dir, "reviews")
    except OSError:
        pruned = None
    return OK, {"run": run, "bundle": state["bundle"], "shadow": state.get("mode") == "codex",
                "workspaces_pruned": pruned, "reviewed": len(state["submitted"]),
                **{name: outcomes.count(name) for name in VERDICTS},
                "downgraded": sorted({row["downgrade"] for row in state["submitted"] if row.get("downgrade")}),
                "dropped": state["dropped"], "unsubmitted": sorted(state["pending"]), "backlog_remaining": remaining,
                "lease": released, "jobs": sorted({row["job"] for row in state["submitted"] if row.get("job")}),
                # What the writer would have concluded (a dry-run commits nothing): compare with Codex.
                "dry_run": [{key: row.get(key) for key in ("path", "base", "verdict", "outcome", "downgrade")}
                            for _path, row in sorted((state.get("dry_run") or {}).items())],
                "triage": {"reviewed": len(record["submitted"]), "upheld": verdicts.count(("upheld", "done")),
                           "missed": verdicts.count(("missed", "done")),
                           "void": sum(status == "void" for _verdict, status in verdicts),
                           "dropped": record["dropped"], "unsubmitted": sorted(record["pending"]),
                           "unfetched": record["unfetched"], "backlog_remaining": triaged and triaged.get("total")}}


# --- the command line ----------------------------------------------------------------------------


def add_parser(sub, common: dict) -> None:
    """Register ``ai-wiki review`` and its verbs on the root parser's subcommands."""
    review = sub.add_parser(
        "review", help="the auditor loop: begin, next, evidence, verdict, submit, end",
        command_path="ai-wiki review", epilog=cli._examples(
            'ai-wiki -b solvely-wiki review begin --run "$MULTICA_ISSUE_ID"', "ai-wiki review next --json",
            "ai-wiki review verdict metrics/x.md verified --note 'every figure is in S1'", "ai-wiki review submit"),
        **common)
    verbs = review.add_subparsers(dest="action", required=True)

    def verb(name: str, help_: str, *examples: str) -> argparse.ArgumentParser:
        parser = verbs.add_parser(name, help=help_, command_path=f"ai-wiki review {name}",
                                  epilog=cli._examples(*examples), **common)
        parser.add_argument("--state-dir", type=Path, default=cli._STATE_DIR, help=f"default: {cli._STATE_DIR}")
        parser.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
        return parser

    begin_ = verb("begin", "doctor, the auditor lease and a workspace (exit 4 fails closed)",
                  'ai-wiki -b solvely-wiki review begin --run "$MULTICA_ISSUE_ID"')
    begin_.add_argument("--run", required=True, type=cli._run, help="the run id (X-AIWiki-Run)")
    begin_.add_argument("--max", type=cli._positive, default=20, help="concepts this run may take (default: 20)")
    begin_.add_argument("--deadline", type=maint._duration, default=DEADLINE_S,
                        help="take no concept or triage item after this, from the first begin (default: 150m)")
    begin_.add_argument("--triage-reserve", type=maint._duration, default=TRIAGE_RESERVE_S,
                        help="stop taking concepts this long before --deadline, for the triage review (default: 30m)")
    begin_.add_argument("--dir", type=Path, help="the workspace (default: <state-dir>/reviews/<run>/ws)")
    begin_.add_argument("--as-human", action="store_true",
                        help="a human reviewer (human: principal with human_verify): verdicts are human-reviewed")
    begin_.add_argument("--triage-max", type=cli._positive, default=TRIAGE_MAX,
                        help=f"triage items this run may take (default: {TRIAGE_MAX})")
    take = verb("next", "take the next backlog concept (exit 10 backlog empty, 11 budget or deadline spent)",
                "ai-wiki review next --json", "ai-wiki review next --path metrics/x.md  # a human run")
    take.add_argument("--path", help="a human run: take this concept, in the backlog or not")
    check = verb("evidence", "re-check the concept's frozen evidence; re-read its Git parts on this host; "
                             "list related uncited sources",
                 "ai-wiki review evidence metrics/x.md --config ~/.ai-wiki/maint.json")
    check.add_argument("path")
    check.add_argument("--config", type=Path, default=maint.CONFIG,
                       help=f"repos.root settings (default: {maint.CONFIG})")
    record = verb("verdict", "record a verdict; corrected takes your workspace edit of the concept",
                  "ai-wiki review verdict metrics/x.md unverified --note 'the lift is not in S1'")
    record.add_argument("path")
    record.add_argument("verdict", choices=VERDICTS)
    record.add_argument("--note", required=True, help="what you checked, for the receipt (never evidence)")
    send = verb("submit", "send the recorded verdicts (exit 6 some dropped, 8 no final answer)",
                "ai-wiki review submit", "ai-wiki review submit --dry-run --json")
    send.add_argument("--dry-run", action="store_true", help="ask the writer's verdict; commit nothing")
    send.add_argument("--wait", type=cli._positive, default=600, help="seconds to follow a queued job (default: 600)")
    finish = verb("end", "release the lease and summarize the run", 'ai-wiki review end --run "$MULTICA_ISSUE_ID"')
    finish.add_argument("--run", required=True, type=cli._run)
    triage_ = verbs.add_parser("triage", help="re-check the maintainer's skips and duplicates: next, verdict, submit",
                               command_path="ai-wiki review triage", epilog=cli._examples(
                                   "ai-wiki review triage next --json",
                                   "ai-wiki review triage verdict it_3f2a9c1b7d10 upheld --note 'nothing durable'"),
                               **common)
    triage_verbs = triage_.add_subparsers(dest="triage_action", required=True)

    def triage_verb(name: str, help_: str, *examples: str) -> argparse.ArgumentParser:
        parser = triage_verbs.add_parser(name, help=help_, command_path=f"ai-wiki review triage {name}",
                                         epilog=cli._examples(*examples), **common)
        parser.add_argument("--state-dir", type=Path, default=cli._STATE_DIR, help=f"default: {cli._STATE_DIR}")
        parser.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
        return parser

    triage_verb("next", "take the next skip or duplicate to re-check, with its frozen files (exit 10 none, "
                        "11 budget or deadline spent)", "ai-wiki review triage next --json")
    judged = triage_verb("verdict", "record upheld, or missed with the claims it lost",
                         "ai-wiki review triage verdict it_3f2a9c1b7d10 upheld --note 'only lockfile bumps'",
                         "ai-wiki review triage verdict it_3f2a9c1b7d10 missed --note 'a member decision' "
                         "--missed-file claims.json")
    judged.add_argument("item")
    judged.add_argument("verdict", choices=("upheld", "missed"))
    judged.add_argument("--note", required=True, help="why, for the receipt (never evidence)")
    judged.add_argument("--missed-file", type=Path,
                        help="missed: a JSON list of {claim, evidence: {file, lines: [first, last]}, target}")
    triage_verb("submit", "send the recorded triage verdicts (exit 6 some dropped, 8 no final answer)",
                "ai-wiki review triage submit --json")


def command(a: argparse.Namespace, selected: str | None) -> int:
    from aiwiki.cli import workspace

    state_dir = Path(a.state_dir).expanduser().resolve()
    try:
        if a.action == "begin":
            code, result = begin(maint._bundle(selected), run=a.run, state_dir=state_dir, max_reviews=a.max,
                                 workspace_dir=a.dir, as_human=a.as_human, triage_max=a.triage_max,
                                 deadline_s=a.deadline, triage_reserve_s=a.triage_reserve)
        elif a.action == "next":
            code, result = next_concept(state_dir, selected, a.path)
        elif a.action == "evidence":
            code, result = evidence(state_dir, selected, a.path, a.config.expanduser())
        elif a.action == "verdict":
            code, result = verdict(state_dir, selected, a.path, a.verdict, a.note)
        elif a.action == "submit":
            code, result = submit(state_dir, selected, dry_run=a.dry_run, wait=a.wait)
        elif a.action == "triage" and a.triage_action == "next":
            code, result = triage_next(state_dir, selected)
        elif a.action == "triage" and a.triage_action == "verdict":
            code, result = triage_verdict(state_dir, selected, a.item, a.verdict, a.note,
                                          a.missed_file and a.missed_file.expanduser())
        elif a.action == "triage":
            code, result = triage_submit(state_dir, selected)
        else:
            code, result = end(state_dir, selected, a.run)
    except (MaintError, workspace.WorkspaceError, OSError, ValueError) as exc:
        code = FAILED if isinstance(exc, (OSError, ValueError)) else exc.code
        if a.json:
            print(json.dumps({"error": str(exc), "detail": getattr(exc, "detail", None)}, ensure_ascii=False))
            return code
        verb = f"{a.action} {a.triage_action}" if a.action == "triage" else a.action
        cli._fail(str(exc), help_command=f"ai-wiki review {verb} --help", code=code)
    maint._print(result, a.json, f"triage_{a.triage_action}" if a.action == "triage" else a.action)
    return code
