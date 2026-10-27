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
import contextlib
import hashlib
import json
import re
from datetime import UTC, datetime
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
          as_human: bool = False, triage_max: int = TRIAGE_MAX) -> tuple[int, dict]:
    """doctor → auditor lease → workspace pull → the backlog's size. ``as_human`` preflights a
    human reviewer (design §5.5), whom the writer does not hold to the backlog."""
    from aiwiki.cli import doctor, update, workspace

    folder = _folder(state_dir, run)
    state = maint._read(folder / "review.json") or {  # a begin repeated within the run keeps its record
        "run": run, "bundle": bundle, "max": max_reviews, "taken": [], "pending": {}, "submitted": [], "dropped": []}
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
    if not started.acquire():
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
                "budget": {"max": state["max"], "taken": len(state["taken"])},
                "triage": {"backlog": triaged and triaged.get("total"), "epoch": triaged and triaged.get("epoch"),
                           "max": _triage(state)["max"], "taken": len(_triage(state)["taken"])},
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
    if len(state["taken"]) >= state["max"]:
        return BUDGET, {"stopped": "budget", "taken": len(state["taken"]), "pending": len(state["pending"]),
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
        "budget": {"taken": len(state["taken"]), "max": state["max"], "pending": len(state["pending"])},
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


def _related(root: Path, document, frozen: dict, cited: set[str]) -> list[dict]:
    """Up to ``RELATED`` frozen sources of the workspace (``sources/`` but its inbox) that the
    concept does not cite and that state at least two of its identifiers, most first. An
    identifier that more than ``COMMON`` sources state is dropped. Client-side only: the writer
    keeps no index, so this reads the workspace the run pulled."""
    identifiers = _identifiers(document)
    holds: dict[str, set[str]] = {}
    for rel, recorded in frozen.items():
        file = root / str(rel)
        if not str(rel).startswith("sources/") or "inbox" in Path(str(rel)).parts or not file.is_file() \
                or file.is_symlink():
            continue
        data = file.read_bytes()
        if hashlib.sha256(data).hexdigest() != recorded:
            continue  # not the bytes the service froze: no evidence
        text = data.decode("utf-8", errors="replace")
        holds[str(rel)] = {name for name in identifiers if name in text and re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text)}
    stated = {name: sum(name in found for found in holds.values()) for name in identifiers}
    rows = [{"source": rel, "matches": len(names), "identifiers": sorted(names)}
            for rel, found in holds.items() if rel not in cited
            and len(names := {name for name in found if stated[name] <= COMMON}) >= 2]
    return sorted(rows, key=lambda row: (-row["matches"], row["source"]))[:RELATED]


def evidence(state_dir: Path, bundle: str | None, path: str, config: Path) -> tuple[int, dict]:
    """Re-check the concept's cited sources: the frozen bytes against ``sources/.hashes.yaml``,
    and each Git part of a packet re-read from this host's checkouts against its recorded sha256.
    A re-read copy lands in the run's ``evidence/``. ``related`` lists frozen sources the concept
    does not cite that state its identifiers: newer evidence may contradict it. The findings are
    for the note only: the writer never trusts a reviewer's word about evidence (design §5.4)."""
    from aiwiki.engine.document import parse_document
    from aiwiki.engine.scan_sources import _source_resource_rel

    state = _load(state_dir, bundle)
    root = Path(state["workspace"])
    concept = root / path
    if path not in state["taken"] or not concept.is_file():
        raise MaintError(f"{path} is not a concept this run took; run ai-wiki review next", USAGE)
    ledger = root / "sources" / ".hashes.yaml"  # {sources/<name>: sha256}, written by the service
    frozen = yaml.safe_load(ledger.read_text(encoding="utf-8")) if ledger.is_file() else {}
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
    return OK, {"path": path, "evidence": rows, "related": _related(root, document, frozen, cited)}


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


def _triage(state: dict, max_items: int = TRIAGE_MAX) -> dict:
    """The run's triage record, made with ``max_items`` the first time (as ``max``, a begin
    repeated within the run keeps it); a run begun by an older CLI gets the default budget."""
    return state.setdefault("triage", {"max": max_items, "taken": [], "entries": {}, "pending": {},
                                       "submitted": [], "dropped": [], "pulled": False})


def _triage_backlog(bundle: str, limit: int = 1000) -> dict | None:
    """The writer's triage backlog; None from a writer without the route (older than 0.3.6)."""
    try:
        return maint._call("GET", "/audit/backlog/triage", bundle=bundle, params={"limit": limit})[1]
    except MaintError as exc:
        if exc.status != 404:
            raise
        return None


def triage_next(state_dir: Path, bundle: str | None) -> tuple[int, dict]:
    """The first triage item this run has not taken, its frozen evidence files fetched into
    ``triage/<item>/`` (checked by sha256), and the concept a duplicate_of names in the workspace."""
    from aiwiki.cli import workspace

    state = _load(state_dir, bundle)
    record = _triage(state)
    tail = f"ai-wiki -b {state['bundle']} review triage submit" if record["pending"] else \
        f"ai-wiki -b {state['bundle']} review end --run {state['run']}"
    if len(record["taken"]) >= record["max"]:
        return BUDGET, {"stopped": "budget", "taken": len(record["taken"]), "pending": len(record["pending"]),
                        "next": tail}
    found = _triage_backlog(state["bundle"]) or {}
    entry = next((row for row in found.get("items") or [] if row["item"] not in record["taken"]), None)
    if entry is None:
        return EMPTY, {"stopped": "backlog_empty", "epoch": found.get("epoch"), "pending": len(record["pending"]),
                       "next": tail}
    item = entry["item"]
    if not changeset._WORK_ITEM.fullmatch(item):  # it names a folder here
        raise MaintError(f"the triage backlog names an unusable item {item!r}")
    folder, files = _folder(state_dir, state["run"]) / "triage" / item, []
    for file in entry["files"]:
        if not changeset._ITEM_FILE.fullmatch(f"{item}/{file['name']}"):
            raise MaintError(f"triage item {item} names an unusable file {file['name']!r}")
        status, _headers, data = cli._http("GET", f"/maint/items/{item}/files/{quote(file['name'], safe='')}",
                                           bundle=state["bundle"])
        if status != 200 or hashlib.sha256(data).hexdigest() != file["sha256"]:
            raise MaintError(f"evidence {item}/{file['name']} could not be fetched intact ({status})")
        (folder / file["name"]).parent.mkdir(parents=True, exist_ok=True)
        (folder / file["name"]).write_bytes(data)
        files.append({"name": file["name"], "bytes": file["bytes"], "path": str(folder / file["name"])})
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
                                         ok=(200, 400, 409, 413, 422))
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
        refused = {error.get("item"): error.get("code") for error in answer.get("errors") or []
                   if isinstance(error, dict)}
        if not refused.keys() & batch.keys():  # lease_required or a malformed request: nothing to drop
            refusal = MaintError(f"the writer refused the triage reviews ({status}): " + "; ".join(
                f"{error.get('code')}: {error.get('message')}" for error in answer.get("errors") or []
                if isinstance(error, dict)), FAILED, status=status, detail=answer.get("errors"))
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
    return OK, {"run": run, "bundle": state["bundle"], "shadow": state.get("mode") == "codex",
                "reviewed": len(state["submitted"]), **{name: outcomes.count(name) for name in VERDICTS},
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
                           "backlog_remaining": triaged and triaged.get("total")}}


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
    begin_.add_argument("--dir", type=Path, help="the workspace (default: <state-dir>/reviews/<run>/ws)")
    begin_.add_argument("--as-human", action="store_true",
                        help="a human reviewer (human: principal with human_verify): verdicts are human-reviewed")
    begin_.add_argument("--triage-max", type=cli._positive, default=TRIAGE_MAX,
                        help=f"triage items this run may take (default: {TRIAGE_MAX})")
    take = verb("next", "take the next backlog concept (exit 10 backlog empty, 11 budget spent)",
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
                        "11 budget spent)", "ai-wiki review triage next --json")
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
                                 workspace_dir=a.dir, as_human=a.as_human, triage_max=a.triage_max)
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
