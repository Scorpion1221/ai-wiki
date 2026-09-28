"""The auditor's loop (design §3, §5): ``ai-wiki review …``.

    begin     doctor --role auditor, the auditor lease, a workspace of the published bundle
    next      the next concept the writer's backlog lists, with its content hash and evidence
    evidence  re-check the concept's frozen evidence, and re-read its Git parts from this host
    verdict   record verified, corrected (the workspace edit) or unverified, with a note
    submit    send the recorded verdicts as audit changesets of at most 5 reviews
    end       release the lease and summarize the run

The writer derives the backlog and judges every verdict (§5.4): it stamps ``verified`` and
``generated``, restores what a review may not change, and downgrades a correction that adds
anything to unverified. Nothing here reads a curator's hand-off: only the concept, its cited
sources and what this host re-reads. A run keeps ``<state-dir>/reviews/<run>/review.json`` and
its workspace ``ws/``; ``reviews/current-<bundle>.json`` names the bundle's run.

Exit codes: 0 ok, 1 an unexpected failure, 2 usage, 4 preflight failed, 6 some reviews were
dropped (rejected or conflicting), 8 no final answer (resend with submit), 10 the backlog
holds nothing more, 11 the run's budget is spent.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path

import yaml

from aiwiki.cli import main as cli
from aiwiki.cli import maint
from aiwiki.cli.maint import MaintError
from aiwiki.runtime import changeset
from aiwiki.runtime.changeset import VERDICTS

OK, FAILED, USAGE, PREFLIGHT, DROPPED, TRANSIENT, EMPTY, BUDGET = 0, 1, 2, 4, 6, 8, 10, 11
BATCH = 5  # reviews per audit changeset (design §2.3)


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


def begin(bundle: str, *, run: str, state_dir: Path, max_reviews: int, workspace_dir: Path | None) -> tuple[int, dict]:
    """doctor → auditor lease → workspace pull → the backlog's size."""
    from aiwiki.cli import doctor, workspace

    folder = _folder(state_dir, run)
    state = maint._read(folder / "review.json") or {  # a begin repeated within the run keeps its record
        "run": run, "bundle": bundle, "max": max_reviews, "taken": [], "pending": {}, "submitted": [], "dropped": []}
    if state["bundle"] != bundle:
        raise MaintError(f"run {run} reviews bundle {state['bundle']!r}, not {bundle!r}", USAGE)
    checked = doctor.run("auditor", bundle=bundle, state_dir=state_dir, skills_dir=None)
    if not checked["ok"]:
        failed = [row for row in checked["checks"] if not row["ok"]]
        return PREFLIGHT, {"run": run, "failed": "doctor", "checks": failed}
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
    _save(state_dir, state)
    maint._write(_pointer(state_dir, bundle), {"run": run, "bundle": bundle})
    return OK, {"run": run, "bundle": bundle, "workspace": state["workspace"], "base_revision": pulled["base_revision"],
                "lease_expires_at": lease.get("expires_at"), "mode": found.get("mode"), "epoch": found.get("epoch"),
                "backlog": found.get("total"), "seed": found.get("seed"),
                "budget": {"max": state["max"], "taken": len(state["taken"])},
                "next": f"ai-wiki -b {bundle} review next --json"}


def next_concept(state_dir: Path, bundle: str | None) -> tuple[int, dict]:
    """The first backlog concept this run has not taken, as the published workspace holds it."""
    from aiwiki.cli import workspace

    state = _load(state_dir, bundle)
    if len(state["taken"]) >= state["max"]:
        return BUDGET, {"stopped": "budget", "taken": len(state["taken"]), "pending": len(state["pending"]),
                        "next": f"ai-wiki -b {state['bundle']} review submit" if state["pending"] else
                        f"ai-wiki -b {state['bundle']} review end --run {state['run']}"}
    found = _backlog(state["bundle"])
    entry = next((row for row in found.get("concepts") or [] if row["path"] not in state["taken"]), None)
    if entry is None:
        return EMPTY, {"stopped": "backlog_empty", "pending": len(state["pending"]),
                       "next": f"ai-wiki -b {state['bundle']} review submit" if state["pending"] else
                       f"ai-wiki -b {state['bundle']} review end --run {state['run']}"}
    root = Path(state["workspace"])
    if workspace.load(root)["base_revision"] != found.get("revision"):
        workspace.pull(root, state["bundle"])  # verdicts keep their own copy: the workspace stays clean
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


def evidence(state_dir: Path, bundle: str | None, path: str, config: Path) -> tuple[int, dict]:
    """Re-check the concept's cited sources: the frozen bytes against ``sources/.hashes.yaml``,
    and each Git part of a packet re-read from this host's checkouts against its recorded sha256.
    A re-read copy lands in the run's ``evidence/``. The findings are for the note only: the
    writer never trusts a reviewer's word about evidence (design §5.4)."""
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
    rows, out = [], _folder(state_dir, state["run"]) / "evidence"
    sources = parse_document(concept.read_text(encoding="utf-8")).frontmatter.get("sources") or []
    for source in sources if isinstance(sources, list) else []:
        resource = source.get("resource") if isinstance(source, dict) else None
        rel = _source_resource_rel(resource.strip(), path) if isinstance(resource, str) else None
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
    return OK, {"path": path, "evidence": rows}


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
    records the rest. A dry-run asks the writer's verdict and changes nothing."""
    from aiwiki.cli import workspace

    state = _load(state_dir, bundle)
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
        results += [{"job": answer.get("id"), **{key: row.get(key) for key in ("path", "outcome", "downgrade")}}
                    for row in answer.get("reviews") or []]
        for path in batch:
            pending.pop(path)
    if not dry_run:
        state["pending"] = pending
        state["submitted"] += results
        state["dropped"] += dropped
        _save(state_dir, state)
        if results:
            with contextlib.suppress(workspace.WorkspaceError, OSError):
                workspace.pull(root, state["bundle"])
    return code, {"dry_run": dry_run, "reviews": results, "dropped": dropped,
                  "pending": len(state["pending"])}  # a dry-run keeps every verdict pending


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
    if (maint._read(_pointer(state_dir, state["bundle"])) or {}).get("run") == run:
        _pointer(state_dir, state["bundle"]).unlink()
    return OK, {"run": run, "bundle": state["bundle"], "reviewed": len(state["submitted"]),
                **{name: outcomes.count(name) for name in VERDICTS},
                "downgraded": sorted({row["downgrade"] for row in state["submitted"] if row.get("downgrade")}),
                "dropped": state["dropped"], "unsubmitted": sorted(state["pending"]), "backlog_remaining": remaining,
                "lease": released, "jobs": sorted({row["job"] for row in state["submitted"] if row.get("job")})}


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
    verb("next", "take the next backlog concept (exit 10 backlog empty, 11 budget spent)", "ai-wiki review next --json")
    check = verb("evidence", "re-check the concept's frozen evidence; re-read its Git parts on this host",
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


def command(a: argparse.Namespace, selected: str | None) -> int:
    from aiwiki.cli import workspace

    state_dir = Path(a.state_dir).expanduser().resolve()
    try:
        if a.action == "begin":
            code, result = begin(maint._bundle(selected), run=a.run, state_dir=state_dir, max_reviews=a.max,
                                 workspace_dir=a.dir)
        elif a.action == "next":
            code, result = next_concept(state_dir, selected)
        elif a.action == "evidence":
            code, result = evidence(state_dir, selected, a.path, a.config.expanduser())
        elif a.action == "verdict":
            code, result = verdict(state_dir, selected, a.path, a.verdict, a.note)
        elif a.action == "submit":
            code, result = submit(state_dir, selected, dry_run=a.dry_run, wait=a.wait)
        else:
            code, result = end(state_dir, selected, a.run)
    except (MaintError, workspace.WorkspaceError, OSError, ValueError) as exc:
        code = FAILED if isinstance(exc, (OSError, ValueError)) else exc.code
        if a.json:
            print(json.dumps({"error": str(exc), "detail": getattr(exc, "detail", None)}, ensure_ascii=False))
            return code
        cli._fail(str(exc), help_command=f"ai-wiki review {a.action} --help", code=code)
    maint._print(result, a.json, a.action)
    return code
