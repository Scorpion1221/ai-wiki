"""The maintainer's loop (design §3, §4): ``ai-wiki maint …``.

    begin          doctor, the maintainer lease, Codex audit resubmission, workspace pull, collect
    collect        run the collectors; freeze their evidence as work items, then move the cursors
    next           claim the next work item and fetch its evidence into the workspace
    add-evidence   freeze one more file into the item: a Git blob at a commit, or a Multica issue
    skip | park | split   close the item without a changeset, hand it back, or divide it
    end            release the lease and render the run's deterministic report
    status         the writer's SLO snapshot
    import-v4 | export-v4 | import-ledger   migrate from and back to the P0 checkpoint and ledger

A run keeps one directory, ``<state-dir>/runs/<run>/``: ``run.json`` (budget, taken items,
collection results), ``proposals.jsonl`` (what ``ai-wiki propose`` sent during the run) and
the workspace ``ws/``. ``<state-dir>/runs/current-<bundle>.json`` names the bundle's run the
other verbs act on: the one of ``-b``, else the only one begun here (two agents sharing a
state directory, say production and its shadow canary, pass ``-b``). Progress itself lives
on the writer (items and cursors), so any host can take over.
The collectors read ``--config`` (default ``~/.ai-wiki/maint.json``)::

    {"repos": {"root": "/srv/reference", "exclude_remotes": ["<the bundle's own repo>"],
               "registry": "multica", "required_remotes": [], "branch_overrides": {},
               "priority_prefixes": ["tasks", "memory", "docs/solutions"], "git_timeout": 120},
     "issues": {"autopilot": "<id>", "exclude_agents": ["<maintainer>", "<auditor>"],
                "exclude_issues": [], "since": {"updated_at": "…", "id": "0"}},
     "audits": {"resubmit": true}}

``audits.resubmit: false`` keeps ``begin`` from resubmitting Codex audits (a shadow bundle).

Exit codes: 0 ok, 1 an unexpected failure, 2 usage, 3 ``end`` saw new needs_human items,
4 preflight failed, 5 a collector failed, 10 the queue is empty, 11 the run's budget is
spent, 12 the workspace holds changes.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from aiwiki.cli import main as cli
from aiwiki.cli.toon import emit, object_lines, table_lines

OK, FAILED, USAGE, NEEDS_HUMAN, PREFLIGHT, PARTIAL, EMPTY, BUDGET, DIRTY = 0, 1, 2, 3, 4, 5, 10, 11, 12
COLLECTORS = ("repos", "issues")
CONFIG = Path("~/.ai-wiki/maint.json")  # expanded when used
AUDIT_ATTEMPTS = 3  # failed Codex audits of one parent before it needs a human (design §4.7)
BATCH = 20  # items per POST /maint/items
LIST_LIMIT = 1000  # the most items GET /maint/items returns at once
BRIEF_BYTES = 2048
SKIP_REASONS = ("no_durable_knowledge", "insufficient_evidence", "out_of_scope")
# The writer's retryable park classes; a non-retryable one (auth, disk, input) is not the agent's call.
PARK_CLASSES = ("model_output", "context", "transient", "capacity", "conflict", "timeout", "internal")
UNFINISHED = ("ready", "parked", "in_progress", "needs_human")  # what export-v4 hands back to ``maintain``
# Neither may start with "-": git and multica would read it as an option.
_GIT_REF = re.compile(
    r"(?P<remote>[^-].*)@(?P<commit>[0-9a-f]{7,40}):(?P<path>[^#]+)(?:#L(?P<first>\d+)(?:-L?(?P<last>\d+))?)?")
_ISSUE_REF = re.compile(r"issue:(?P<issue>[^#\s-][^#\s]*)(?:#(?P<comment>[^\s-]\S*))?")


class MaintError(Exception):
    """A verb that cannot go on; ``code`` is its exit code and ``status`` the writer's answer."""

    def __init__(self, message: str, code: int = FAILED, *, status: int | None = None, detail: object = None):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, path)


# --- the writer --------------------------------------------------------------------------------


def _call(method: str, route: str, *, bundle: str | None, run: str | None = None, body: object = None,
          params: dict | None = None, headers: dict | None = None, ok: tuple[int, ...] = (200,)) -> tuple[int, dict]:
    """One request to the writer: its status and JSON object. A status outside ``ok`` raises."""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    sent = {**({"X-AIWiki-Run": run} if run else {}), **(headers or {})}
    try:
        status, _headers, raw = cli._http(method, route, bundle=bundle, params=params, data=data,
                                          headers=sent or None, timeout=300)
    except OSError as exc:
        raise MaintError(f"{method} {route}: the writer is unreachable ({exc})") from None
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {"detail": raw.decode("utf-8", errors="replace")[:500]}
    payload = payload if isinstance(payload, dict) else {"detail": payload}
    if status not in ok:
        detail = payload.get("detail")
        message = detail.get("message") if isinstance(detail, dict) else detail
        code = PREFLIGHT if status in (401, 403) else USAGE if status == 400 else FAILED  # 400: the input
        raise MaintError(f"{method} {route} answered {status}" + (f": {message}" if message else ""), code,
                         status=status, detail=detail)
    return status, payload


def _cursor(bundle: str, name: str) -> dict | None:
    status, record = _call("GET", f"/maint/cursors/{name}", bundle=bundle, ok=(200, 404))
    return record if status == 200 else None


def _move(bundle: str, run: str | None, name: str, record: dict | None, value: dict) -> dict:
    """Compare-and-swap the cursor from ``record`` (None: it must not exist yet) to ``value``."""
    condition = {"If-Match": f'"{record["etag"]}"'} if record else {"If-None-Match": "*"}
    return _call("PUT", f"/maint/cursors/{name}", bundle=bundle, run=run, body={"value": value}, headers=condition)[1]


def _held(bundle: str, run: str) -> str | None:
    """The item ``run`` has in progress on the writer, if any."""
    rows = _call("GET", "/maint/items", bundle=bundle, params={"status": "in_progress", "limit": LIST_LIMIT})[1]
    return next((row["id"] for row in rows.get("items") or [] if row.get("current_run") == run), None)


def _enqueue(bundle: str, run: str | None, items: list[dict]) -> dict:
    """POST planned items with their bytes, in batches; the writer dedupes by item_key."""
    counts = {"created": 0, "merged": 0, "duplicate": 0}
    for start in range(0, len(items), BATCH):
        body = [{"origin": item["origin"], "topic_key": item["topic_key"], "priority": item["priority"],
                 "brief": item["brief"], "files": [{"name": file["name"], "origin": file["origin"],
                                                    "content_b64": base64.b64encode(file["data"]).decode()}
                                                   for file in item["files"]]}
                for item in items[start:start + BATCH]]
        answer = _call("POST", "/maint/items", bundle=bundle, run=run, body={"items": body})[1]
        for key in counts:
            counts[key] += int(answer.get(key) or 0)
    return counts


# --- the run on this host ------------------------------------------------------------------------


def _slug(run: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", run)[:80] + "-" + hashlib.sha256(run.encode()).hexdigest()[:8]


def run_dir(state_dir: Path, run: str) -> Path:
    return state_dir / "runs" / _slug(run)


def _pointer(state_dir: Path, bundle: str) -> Path:
    return state_dir / "runs" / f"current-{_slug(bundle)}.json"


def _currents(state_dir: Path) -> list[dict]:
    """``{run, bundle}`` of every run begun here and not yet ended, one per bundle."""
    return [current for path in sorted((state_dir / "runs").glob("current-*.json")) if (current := _read(path))]


def _current(state_dir: Path, bundle: str | None) -> dict:
    """The run of ``bundle`` (``-b``), else of the only bundle with a run here; ``{}`` for none."""
    if bundle:
        return _read(_pointer(state_dir, bundle)) or {}
    currents = _currents(state_dir)
    if len(currents) > 1:
        raise MaintError("runs of " + ", ".join(sorted(str(c.get("bundle")) for c in currents))
                         + " are active here; pass -b <bundle>", USAGE)
    return currents[0] if currents else {}


def load_run(state_dir: Path, run: str | None = None, bundle: str | None = None) -> dict:
    """The run's record; without ``run`` the current run's. ``bundle`` (``-b``) must match it."""
    if run is None:
        run = _current(state_dir, bundle).get("run")
        if not run:
            raise MaintError("no maintenance run is active here; run: ai-wiki maint begin --run <id>", USAGE)
    state = _read(run_dir(state_dir, run) / "run.json")
    if state is None:
        raise MaintError(f"run {run} has not begun here; run: ai-wiki maint begin --run {run}", USAGE)
    if bundle and bundle != state["bundle"]:
        raise MaintError(f"run {run} maintains bundle {state['bundle']!r}, not {bundle!r}", USAGE)
    return state


def _save_run(state_dir: Path, state: dict) -> None:
    _write(run_dir(state_dir, state["run"]) / "run.json", state)


def record(state_dir: Path, request: dict, code: int, result: dict) -> None:
    """Note what ``propose`` sent during a current run, for its report; outside a run, nothing."""
    current = next((c for c in _currents(state_dir) if c.get("run") and c["run"] == request.get("run")), None)
    if current is None:
        return
    row = {"at": _iso(_now()), "items": request.get("work_items") or [], "exit": code, "job": result.get("id"),
           "status": result.get("status"), "http_status": result.get("http_status"),
           # A local rejection (never sent) is the gate's own verdict: no job id, a validation block.
           "sent": bool(result.get("id")) or "validation" not in result,
           "errors": sorted({error["code"] for error in result.get("errors") or []
                             if isinstance(error, dict) and isinstance(error.get("code"), str)})}
    path = run_dir(state_dir, current["run"]) / "proposals.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")


def _proposals(state_dir: Path, run: str) -> list[dict]:
    path = run_dir(state_dir, run) / "proposals.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else ():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue  # a line cut short by a crash
    return rows


def _config(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = _read(path)
    if value is None:
        raise MaintError(f"{path} is not a JSON object", USAGE)
    return value


# --- collection ----------------------------------------------------------------------------------


def _collect_repos(bundle: str, run: str | None, work: Path, cache: Path, config: object) -> dict:
    from aiwiki.maint import collect_issues, collect_repos, planner

    if not isinstance(config, dict) or not config.get("root"):
        raise MaintError("repos: set repos.root in the maint config")
    record_ = _cursor(bundle, "repos")
    previous = (record_ or {}).get("value") or None
    prefixes = tuple(config.get("priority_prefixes") or planner.TOPIC_PREFIXES)
    work.mkdir(parents=True, exist_ok=True)
    argv = ["--root", str(Path(config["root"]).expanduser()), "--cache-dir", str(cache / "repos"),
            "--output", str(work / "scan.json"), "--quiet", "--git-timeout", str(config.get("git_timeout", 120))]
    if previous:
        _write(work / "checkpoint.json", collect_repos.checkpoint_from_cursor(previous))
        argv += ["--checkpoint-json", str(work / "checkpoint.json")]
    if config.get("registry") == "multica":
        argv += ["--registered-json", str(collect_issues.repo_registry(work / "registry.json"))]
    elif config.get("registered_json"):
        argv += ["--registered-json", str(Path(config["registered_json"]).expanduser())]
    for url in config.get("required_remotes") or ():
        argv += ["--required-remote", url]
    for url, branch in (config.get("branch_overrides") or {}).items():
        argv += ["--branch-override", f"{url}={branch}"]
    for prefix in prefixes:
        argv += ["--priority-prefix", prefix]
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            code = collect_repos.main(argv)
    except SystemExit as exc:  # the scanner's usage errors
        code = exc.code
    if code not in (collect_repos.EXIT_OK, collect_repos.EXIT_PARTIAL):
        detail = stderr.getvalue().strip().splitlines()
        raise MaintError(f"repos: the scanner exited {code}: {detail[-1] if detail else ''}".strip())
    report = json.loads((work / "scan.json").read_text(encoding="utf-8"))
    collected = collect_repos.collect(report, previous=previous, exclude_remotes=config.get("exclude_remotes") or (),
                                      prefixes=prefixes)
    items = planner.plan(collected["candidates"])
    enqueued = _enqueue(bundle, run, items)  # the evidence is frozen before the cursor moves
    after = _move(bundle, run, "repos", record_, collected["cursor"])
    counts = report["counts"]
    failures = [{"remote": row.get("remote_url"), "error": row.get("error")}
                for row in report["repos"] if row.get("state") == "failed"] + [
        {"remote": warning.get("remote_url"), "error": collect_repos.UNLISTED_ERROR}
        for warning in report["warnings"] if warning.get("type") == "checkpoint_repo_unlisted"] + [
        {"remote": row["remote_url"], "error": row["error"]} for row in collected["failed"]]
    return {"status": "partial" if code == collect_repos.EXIT_PARTIAL or failures else "ok",
            **{key: counts.get(key, 0) for key in ("scanned", "changed", "new", "rebaselined", "unchanged")},
            "failed": len(failures), "failures": failures, "noise": collected["counts"]["noise"],
            "candidates": len(items), **enqueued, "cursor": after["updated_at"]}


def _collect_issues(bundle: str, run: str | None, work: Path, _cache: Path, config: object) -> dict:
    from aiwiki.maint import collect_issues, planner

    if not isinstance(config, dict) or not config.get("autopilot"):
        raise MaintError("issues: set issues.autopilot in the maint config")
    record_ = _cursor(bundle, "issues")
    cursor = (record_ or {}).get("value") or config.get("since")
    if not cursor:
        raise MaintError("issues: no issues cursor yet; run ai-wiki maint import-v4, or set issues.since")
    cache_dir = work / _iso(_now()).replace(":", "")  # a fresh cache per listing
    result = collect_issues.collect(
        cursor, autopilot=config["autopilot"], cache_dir=cache_dir, exclude_agents=config.get("exclude_agents") or (),
        current_issue=run, exclude_issues=config.get("exclude_issues") or (),
        settle_seconds=int(config.get("settle_seconds", 120)))
    if result["status"] != "ok":
        return {"status": result["status"], "error": result["error"]}
    items = planner.plan(result["candidates"])
    enqueued = _enqueue(bundle, run, items)
    after = _move(bundle, run, "issues", record_, result["next_cursor"])
    return {"status": "ok", **{key: result["counts"].get(key, 0) for key in ("listed", "changed", "deferred")},
            "candidates": len(items), **enqueued, "cursor": after["updated_at"]}


def collect(bundle: str, *, run: str | None, work: Path, state_dir: Path, config: dict,
            only: tuple[str, ...]) -> dict:
    """Each requested collector's result. A collector that fails leaves its cursor where it was."""
    results = {}
    for name in COLLECTORS:
        if name not in only:
            continue
        collector = _collect_repos if name == "repos" else _collect_issues
        try:
            results[name] = collector(bundle, run, work / name, state_dir / "cache", config.get(name))
        except (MaintError, OSError, ValueError, KeyError) as exc:
            results[name] = {"status": "failed", "error": str(exc)[:500]}
    return results


SUMMED = ("changed", "new", "rebaselined", "deferred", "candidates", "created", "merged", "duplicate")


def _merged(earlier: dict, latest: dict) -> dict:
    """A run's collection over repeated begins: each collector's latest outcome, with what the
    collections found and enqueued summed."""
    merged = dict(earlier)
    for name, result in latest.items():
        before = earlier.get(name) or {}
        merged[name] = {**result, **{key: result.get(key, 0) + before.get(key, 0) for key in SUMMED
                                     if isinstance(result.get(key, 0), int) and isinstance(before.get(key, 0), int)
                                     and (key in result or key in before)}}
    return merged


def _collect_code(results: dict) -> int:
    return OK if all(result.get("status") == "ok" for result in results.values()) else PARTIAL


# --- begin ---------------------------------------------------------------------------------------


def _audits(bundle: str) -> dict:
    """Phase 1–3: resubmit the Codex audits of done ingests still unaudited after an hour.

    As in P0 ``maintain``, a capacity failure never counts and a non-retryable one (auth,
    disk, input) needs a human at once. A parent gets AUDIT_ATTEMPTS counted failures, plus
    one more on each writer build none of them ran on (a fix may be deployed); past that it
    needs a human.
    """
    from aiwiki.runtime.failure import classify

    who = _call("GET", "/whoami", bundle=bundle)[1]
    if (who.get("modes") or {}).get("audit") != "codex":
        return {"mode": (who.get("modes") or {}).get("audit"), "resubmitted": [], "needs_human": []}
    build = (who.get("service") or {}).get("build")
    pending = _call("GET", "/jobs/pending-audit", bundle=bundle, params={"older_than_hours": 1, "limit": 100})[1]
    resubmitted, needs_human, refused = [], [], []
    for job in pending.get("jobs") or []:
        failed = job.get("failed_audit_attempts") or []  # oldest first
        counted = [attempt for attempt in failed if classify(attempt)["class"] != "capacity"]
        builds = {(attempt.get("service") or {}).get("build") for attempt in counted}
        if (failed and not classify(failed[-1])["retryable"]) or (
                len(counted) >= AUDIT_ATTEMPTS and (not build or build in builds)):
            needs_human.append(job["id"])
            continue
        try:
            _call("POST", f"/jobs/{quote(job['id'], safe='')}/audit", bundle=bundle)
        except MaintError as exc:  # one parent the writer refuses never stops the others
            refused.append({"id": job["id"], "error": str(exc)[:300]})
            continue
        resubmitted.append(job["id"])
    return {"mode": "codex", "pending": pending.get("total"), "resubmitted": resubmitted, "needs_human": needs_human,
            "refused": refused}


def begin(bundle: str, *, run: str, state_dir: Path, config: Path, max_items: int, deadline_s: int,
          only: tuple[str, ...], workspace_dir: Path | None) -> tuple[int, dict]:
    """doctor → lease → audit resubmission → workspace pull → collect (design §3)."""
    from aiwiki.cli import doctor, workspace

    settings = _config(config)
    folder = run_dir(state_dir, run)
    state = _read(folder / "run.json")
    if state and state.get("bundle") != bundle:
        raise MaintError(f"run {run} maintains bundle {state['bundle']!r}, not {bundle!r}", USAGE)
    checked = doctor.run("curator", bundle=bundle, state_dir=state_dir, skills_dir=None)
    if not checked["ok"]:
        failed = [row for row in checked["checks"] if not row["ok"]]
        return PREFLIGHT, {"run": run, "failed": "doctor", "checks": failed}
    try:
        lease = _call("POST", "/maint/lease/maintainer", bundle=bundle, run=run)[1]
    except MaintError as exc:
        return PREFLIGHT, {"run": run, "failed": "lease", "error": str(exc), "holder": exc.detail}
    now = _now()
    if state is None:  # a begin repeated within the run keeps its clock, budget and taken items
        state = {"run": run, "bundle": bundle, "started_at": _iso(now),
                 "deadline": _iso(now + timedelta(seconds=deadline_s)), "max_items": max_items, "taken": [],
                 "stopped": None}
    # The writer's clock when the run first took the lease: its sweep and every later
    # needs_human item are stamped at or after it, whatever this host's clock says.
    state["acquired_at"] = state.get("acquired_at") or (lease.get("lease") or {}).get("acquired_at")
    state.update(workspace=str((workspace_dir or folder / "ws").expanduser().resolve()),
                 swept={key: lease.get(key) or [] for key in ("interrupted", "unparked", "build_retry")})
    try:
        if "cursors_before" not in state:  # where the run found the cursors, over repeated begins
            state["cursors_before"] = {name: _cursor(bundle, name) for name in COLLECTORS}
        _save_run(state_dir, state)
        _write(_pointer(state_dir, bundle), {"run": run, "bundle": bundle})
        try:  # a shadow bundle leaves its audits to the admin cron (design §9, phase 2)
            state["audits"] = _audits(bundle) if (settings.get("audits") or {}).get("resubmit", True) else {
                "mode": "skipped", "resubmitted": [], "needs_human": []}
        except MaintError as exc:  # the audit backlog never holds curation back
            state["audits"] = {"error": str(exc), "resubmitted": [], "needs_human": []}
        reclaimed = []
        if (Path(state["workspace"]) / workspace.META / "workspace.json").is_file() and _held(bundle, run) is None:
            reclaimed = _reset(state)  # edits of an item the writer took back (an expired lease)
        pulled = workspace.pull(Path(state["workspace"]), bundle)
    except (MaintError, workspace.WorkspaceError, OSError) as exc:
        with contextlib.suppress(MaintError):  # let the next run take the lease at once
            _call("DELETE", "/maint/lease/maintainer", bundle=bundle, run=run)
        _pointer(state_dir, bundle).unlink(missing_ok=True)
        return PREFLIGHT, {"run": run, "failed": "workspace", "error": str(exc)}
    collected = collect(bundle, run=run, work=folder / "collect", state_dir=state_dir, config=settings, only=only)
    state["collect"] = _merged(state.get("collect") or {}, collected)
    _save_run(state_dir, state)
    queue = _call("GET", "/maint/status", bundle=bundle)[1]
    # A summary of at most ~2 KB; the lists behind its counts are in run.json.
    return _collect_code(collected), {
        "run": run, "bundle": bundle, "workspace": state["workspace"], "base_revision": pulled["base_revision"],
        "lease_expires_at": (lease.get("lease") or {}).get("expires_at"),
        "swept": {key: len(ids) for key, ids in state["swept"].items()}, "reclaimed": len(reclaimed),
        "audits": {key: len(value) if isinstance(value, list) else value for key, value in state["audits"].items()},
        "collect": {name: {key: value for key, value in result.items() if key not in ("failures", "noise")}
                    | ({"failures": result["failures"][:3]} if result.get("failures") else {})
                    for name, result in collected.items()},
        "ready": {kind: group["count"] for kind, group in sorted((queue.get("ready_by_origin") or {}).items())},
        "budget": {"max_items": state["max_items"], "deadline": state["deadline"], "taken": len(state["taken"])},
        "record": str(folder / "run.json"),
        "next": f"ai-wiki maint next --state-dir {state_dir} --json",
    }


# --- the item loop -------------------------------------------------------------------------------


def _commands(state: dict, state_dir: Path, item_id: str) -> list[str]:
    tail = "" if state_dir == cli._STATE_DIR.expanduser() else f" --state-dir {state_dir}"
    ws = state["workspace"]
    return [f"ai-wiki validate --dir {ws} --item {item_id}", f"ai-wiki propose --dir {ws} --item {item_id}{tail}",
            f"ai-wiki maint skip {item_id} --reason <reason>{tail}"]


def next_item(state_dir: Path, bundle: str | None) -> tuple[int, dict]:
    """Claim the next item within the run's budget and fetch its evidence into the workspace."""
    from aiwiki.cli import workspace

    state = load_run(state_dir, bundle=bundle)
    root = Path(state["workspace"])
    changed = workspace.changes(root, workspace.load(root))[0]
    pending = workspace.conflicts(root)
    if changed or pending:
        held = _held(state["bundle"], state["run"])
        return DIRTY, {"error": f"the workspace holds changes of item {held}: propose them, or skip or park it"
                       if held else f"the workspace holds changes but run {state['run']} has no item in progress; "
                                    f"run ai-wiki maint begin --run {state['run']} to reset it",
                       "item": held, "changes": sorted(changed), "conflicts": pending}
    stop = ("budget" if len(state["taken"]) >= state["max_items"]
            else "deadline" if _iso(_now()) >= state["deadline"] else None)
    if stop:
        state["stopped"] = stop
        _save_run(state_dir, state)
        return BUDGET, {"stopped": stop, "taken": len(state["taken"]), "max_items": state["max_items"],
                        "deadline": state["deadline"], "next": "ai-wiki maint end --run " + state["run"]}
    answer = _call("POST", "/maint/items/next", bundle=state["bundle"], run=state["run"])[1]
    item = answer.get("item")
    if not item:
        state["stopped"] = "queue_empty"
        _save_run(state_dir, state)
        return EMPTY, {"stopped": "queue_empty", "ready": 0, "next": "ai-wiki maint end --run " + state["run"]}
    if item["id"] not in state["taken"]:
        state["taken"].append(item["id"])
        _save_run(state_dir, state)
    files, _claimed = workspace._item_evidence(root, state["bundle"], item["id"])
    history = item["attempts"]["history"]
    brief = {
        "item": item["id"], "topic_key": item["topic_key"], "origin": item["origin"].get("kind"),
        "priority": item["priority"], "brief": item["brief"][:300], "resumed": answer.get("resumed"),
        "attempts": {"started": item["attempts"]["started"], "counted": item["attempts"]["counted"],
                     **({"last_class": history[-1].get("class")} if history else {})},
        "ready": answer.get("ready"), "budget": {"taken": len(state["taken"]), "max_items": state["max_items"],
                                                 "deadline": state["deadline"]},
        "evidence_dir": str(root / workspace.META / "items" / item["id"]), "files_total": len(files),
        "files": [{"name": file.name.split("/", 1)[1], "bytes": len(file.data)} for file in files],
        "commands": _commands(state, state_dir, item["id"]),
    }
    while len(json.dumps(brief, ensure_ascii=False).encode()) > BRIEF_BYTES and brief["files"]:
        brief["files"].pop()  # the rest are in evidence_dir
    return OK, brief


def _reset(state: dict) -> list[str]:
    """Put every local change of the workspace back to its pulled base, ``.mine`` copies too."""
    from aiwiki.cli import workspace

    root = Path(state["workspace"])
    changed = workspace.changes(root, workspace.load(root))[0]
    reverted = workspace._revert(root, [{"path": rel, "op": "put"} for rel in changed])
    for rel in workspace.conflicts(root):
        (root / (rel + workspace.MINE)).unlink(missing_ok=True)
    return reverted


def _resolved(item: dict, run: str, body: dict) -> bool:
    """Whether ``run`` already resolved ``item`` as ``body`` asks: a retry after a lost answer."""
    if item["status"] == "in_progress":
        return False
    if body["outcome"] == "parked":
        last = (item["attempts"]["history"] or [{}])[-1]
        return (last.get("run"), last.get("class")) == (run, body["class"])
    return item["status"] == body["outcome"] and (item.get("resolution") or {}).get("run") == run


def resolve(state_dir: Path, bundle: str | None, item_id: str, body: dict) -> tuple[int, dict]:
    """Close, park or split the run's item; its edits leave the workspace with it.

    Repeating the verb after its answer was lost finds the item resolved and only resets.
    """
    state = load_run(state_dir, bundle=bundle)
    route = f"/maint/items/{quote(item_id, safe='')}"
    try:
        item = _call("POST", f"{route}/resolve", bundle=state["bundle"], run=state["run"], body=body)[1]
    except MaintError as exc:
        if not (isinstance(exc.detail, dict) and exc.detail.get("code") == "item_not_in_progress"):
            raise
        item = _call("GET", route, bundle=state["bundle"])[1]
        if not _resolved(item, state["run"], body):
            raise
    resolution = item.get("resolution") or {}
    return OK, {"item": item["id"], "status": item["status"], "reverted": _reset(state),
                **{key: resolution[key] for key in ("reason", "children") if resolution.get(key)},
                "next": "ai-wiki maint next --json"}


def _git_evidence(bundle: str, state_dir: Path, match: re.Match, repos: object) -> dict:
    """A file at a commit of a tracked repository: from its reference checkout under
    ``repos.root`` as the scanner reads it, else from the repo cache, fetching into the cache
    through the checkout's own remote (its credentials) only when neither holds the commit."""
    from aiwiki.maint import collect_repos, planner

    remote, commit, path = match["remote"], match["commit"], match["path"]
    if path.startswith("/") or ".." in path.split("/"):
        raise MaintError(f"{path}: name a path inside the repository", USAGE)
    try:
        identity = collect_repos.canonical_remote(remote)
    except ValueError:
        raise MaintError(f"{remote} is not a repository URL", USAGE) from None
    if identity not in ((_cursor(bundle, "repos") or {}).get("value") or {}):
        raise MaintError(f"{remote} is not a repository the repos collector tracks", USAGE)
    cache = state_dir / "cache" / "repos" / f"{collect_repos.repo_id(identity)}.git"
    try:
        checkouts = []
        if isinstance(repos, dict) and repos.get("root"):
            for checkout in collect_repos.discover_repositories(Path(repos["root"]).expanduser())[0]:
                with contextlib.suppress(collect_repos.ScanError):
                    url = collect_repos.local_remote(checkout)
                    if collect_repos.remote_identity(url)[0] == identity:
                        checkouts.append((checkout, url))
        source = next((checkout for checkout, _url in checkouts if collect_repos.has_commit(checkout, commit)), None)
        if source is None:
            source = cache
            if not (cache / "HEAD").exists():
                cache.mkdir(parents=True, exist_ok=True)
                collect_repos.run_git("init", "--bare", str(cache))
            fetch = checkouts[0][1] if checkouts else remote
            for refspec in (commit, "+refs/heads/*:refs/heads/*"):  # a server may refuse a bare sha
                if collect_repos.has_commit(cache, commit):
                    break
                collect_repos.run_git("fetch", "--quiet", "--no-tags", "--force", fetch, refspec, cwd=cache,
                                      check=False)
        full = collect_repos.run_git("rev-parse", "--verify", f"{commit}^{{commit}}", cwd=source)
        blob = collect_repos.run_git("rev-parse", "--verify", f"{full}:{path}", cwd=source)
        data = collect_repos.read_blob(source, blob)
    except collect_repos.ScanError as exc:
        raise MaintError(f"{remote}@{commit}:{path} cannot be read: {exc}", USAGE) from None
    if b"\0" in data:
        raise MaintError(f"{path} is binary; cite a text file", USAGE)
    text, lines = data.decode(errors="replace"), None
    if match["first"]:
        first, last = int(match["first"]), int(match["last"] or match["first"])
        rows = text.splitlines(keepends=True)
        if not 1 <= first <= last <= len(rows):
            raise MaintError(f"{path} has {len(rows)} lines; L{first}-{last} is outside them", USAGE)
        text, lines = "".join(rows[first - 1:last]), [first, last]
    span = f"-L{lines[0]}-{lines[1]}" if lines else ""
    return planner.evidence_file(
        f"add-{full[:7]}{span}-{collect_repos.snapshot_name(path)}"[:128], text,
        {"kind": "git-file", "remote": collect_repos.display_remote(remote), "commit": full, "path": path,
         "blob": blob, **({"lines": lines} if lines else {})})


def add_evidence(state_dir: Path, bundle: str | None, item_id: str, ref: str, config: Path) -> tuple[int, dict]:
    """Extract evidence deterministically and freeze it into the run's item; no agent text."""
    from aiwiki.cli import workspace
    from aiwiki.maint import collect_issues
    from aiwiki.maint.checkpoint import MulticaError

    state = load_run(state_dir, bundle=bundle)
    settings = _config(config)
    if match := _GIT_REF.fullmatch(ref):
        file = _git_evidence(state["bundle"], state_dir, match, settings.get("repos"))
    elif match := _ISSUE_REF.fullmatch(ref):
        issues = settings.get("issues") or {}
        try:
            file = collect_issues.issue_evidence(
                match["issue"], match["comment"], autopilot=issues.get("autopilot"),
                exclude_agents=issues.get("exclude_agents") or (), current_issue=state["run"],
                exclude_issues=issues.get("exclude_issues") or (),
                cache_dir=run_dir(state_dir, state["run"]) / "evidence" / _iso(_now()))
        except (MulticaError, ValueError, OSError) as exc:
            raise MaintError(f"{ref}: {exc}", USAGE) from None
    else:
        raise MaintError("evidence is <remote>@<commit>:<path>[#Lx-y] or issue:<id>[#<comment>]", USAGE)
    _call("POST", f"/maint/items/{quote(item_id, safe='')}/files", bundle=state["bundle"], run=state["run"],
          body={"name": file["name"], "origin": file["origin"], "content_b64": base64.b64encode(file["data"]).decode()})
    root = Path(state["workspace"])
    workspace._item_evidence(root, state["bundle"], item_id)
    return OK, {"item": item_id, "file": file["name"], "bytes": file["bytes"], "sha256": file["sha256"],
                "truncated": file["origin"]["truncated"], "redactions": file["origin"]["redactions"],
                "path": str(root / workspace.META / "items" / item_id / file["name"])}


# --- end and status ------------------------------------------------------------------------------


def end(state_dir: Path, bundle: str | None, run: str) -> tuple[int, dict, str]:
    """Release the lease and report the run from the writer's receipts (design §4.9)."""
    from aiwiki.maint import report

    state = _read(run_dir(state_dir, run) / "run.json")
    if state is None:
        state = {"run": run, "bundle": bundle, "taken": []}  # begin never got this far: blocked
    if bundle and state.get("bundle") and bundle != state["bundle"]:
        raise MaintError(f"run {run} maintains bundle {state['bundle']!r}, not {bundle!r}", USAGE)
    bundle = state.get("bundle")
    # The backlog as the run leaves it: the Codex audits deferred behind its lease start on release.
    snapshot = _call("GET", "/maint/status", bundle=bundle)[1]
    try:
        released = _call("DELETE", "/maint/lease/maintainer", bundle=bundle, run=run)[1]
    except MaintError as exc:
        released = {"released": False, "error": str(exc)}
    # needs_human as the release leaves it: an interrupted item may reach its attempt cap there.
    snapshot["needs_human"] = _call("GET", "/maint/status", bundle=bundle)[1].get("needs_human") or []
    items = []
    for item_id in state["taken"]:
        status, item = _call("GET", f"/maint/items/{quote(item_id, safe='')}", bundle=bundle, ok=(200, 404))
        if status == 200:
            items.append(item)
    proposals = _proposals(state_dir, run)
    job_ids = {row["job"] for row in proposals if row.get("job") and row.get("status") == "done"}
    job_ids |= {(item.get("resolution") or {}).get("job") for item in items if item["status"] == "curated"} - {None}
    jobs = [job for job_id in sorted(job_ids) for status, job in [
        _call("GET", f"/jobs/{quote(job_id, safe='')}", bundle=bundle, ok=(200, 404))] if status == 200]
    built = report.build(state, items=items, proposals=proposals, jobs=jobs,
                         cursors={name: _cursor(bundle, name) for name in COLLECTORS}, status=snapshot,
                         released=released, ended_at=_iso(_now()))
    markdown = report.render(built)
    folder = run_dir(state_dir, run)
    _write(folder / "report.json", built)
    (folder / "report.md").write_text(markdown, encoding="utf-8")
    if bundle and (_read(_pointer(state_dir, bundle)) or {}).get("run") == run:
        _pointer(state_dir, bundle).unlink()
    code = PREFLIGHT if "collect" not in state else NEEDS_HUMAN if built["needs_human"] else OK
    return code, built, markdown


def _status_lines(status: dict) -> list[list[str]]:
    audit = status.get("audit") or {}
    return [
        object_lines("cursors", {name: cursor and {"updated_at": cursor.get("updated_at"), "age_s": cursor.get("age_s")}
                                 for name, cursor in (status.get("cursors") or {}).items()}),
        table_lines("items", ({"status": name, "count": group.get("count"), "oldest_age_s": group.get("oldest_age_s")}
                              for name, group in sorted((status.get("items") or {}).items())),
                    ("status", "count", "oldest_age_s")),
        table_lines("needs_human", status.get("needs_human") or [], ("id", "topic_key", "since")),
        table_lines("leases", ({"role": role, **(lease or {})} for role, lease in (status.get("leases") or {}).items()),
                    ("role", "holder", "run", "expires_at", "active")),
        object_lines("audit", {key: audit.get(key) for key in ("mode", "pending", "oldest_finished", "queued")}),
    ]


# --- migration -----------------------------------------------------------------------------------


def v4_cursors(value: object) -> dict[str, dict]:
    """The server cursors ``{repos, issues}`` a P0 v4 (or v3) checkpoint holds."""
    from aiwiki.maint import checkpoint, collect_repos

    found = collect_repos.unwrap_checkpoint(value)
    checkpoint.validate(found, found.get("version") if found.get("version") in checkpoint.KEYS else 4)
    repos = {collect_repos.canonical_remote(row["remote_url"]): {
        "branch": row["branch"], "sha": row["sha"], "stale_since": row.get("stale_since"),
        "error": row.get("last_error")} for row in found["repos"].values()}
    return {"repos": dict(sorted(repos.items())),
            "issues": {"updated_at": found["issues"]["updated_at"], "id": found["issues"]["id"]}}


def import_v4(bundle: str, path: Path, *, replace: bool, run: str, as_json: bool) -> int:
    from aiwiki.cli import admin

    try:
        values = v4_cursors(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MaintError(f"{path} holds no usable v4 checkpoint: {exc}", USAGE) from None
    rows = admin.put_cursors(bundle, values, replace=replace, run=run)
    return admin.show_cursors(rows, as_json=as_json, replace_command=f"ai-wiki maint import-v4 {path} --replace")


def export_v4(bundle: str, *, repo_root: str | None, completed_at: str | None) -> dict:
    """A v4 checkpoint of the server cursors, validated as ``checkpoint.py write`` would."""
    from aiwiki.maint import checkpoint, collect_repos

    repos, issues = _cursor(bundle, "repos"), _cursor(bundle, "issues")
    if not repos or not issues:
        raise MaintError("the writer holds no repos and issues cursors to export", USAGE)
    if not repo_root:
        raise MaintError("name the reference root: --repo-root, or repos.root in the maint config", USAGE)
    found = collect_repos.checkpoint_from_cursor(repos["value"])
    found.update(repo_root=repo_root, issues={key: issues["value"].get(key) for key in ("updated_at", "id")},
                 completed_at=completed_at or _iso(_now()))
    try:
        return checkpoint.validate(found)
    except checkpoint.CheckpointError as exc:
        raise MaintError(f"the cursors make no valid v4 checkpoint: {exc}", USAGE) from None


def export_manifest(bundle: str, path: Path) -> dict:
    """Unfinished items as a P0 ``maintain`` manifest, one source per item (design §9 rollback).

    Several text files become one evidence packet, as the gate assembles it; files that do
    not fit one packet become one source each. Each identity names its item: ``maintain``
    treats sources of one identity as versions and would supersede the older of two
    unfinished items of one topic unread. More unfinished items than one listing returns
    fail the export rather than leave some out.
    """
    from aiwiki.runtime import changeset

    folder = path.with_name(path.stem + "-files")
    sources, items = [], []
    for state in UNFINISHED:
        listing = _call("GET", "/maint/items", bundle=bundle, params={"status": state, "limit": LIST_LIMIT})[1]
        if listing.get("truncated"):
            raise MaintError(f"the writer holds {listing.get('total')} {state} items, more than the {LIST_LIMIT} "
                             "one listing returns; the manifest would leave some out")
        items += listing.get("items") or []
    for item in sorted(items, key=lambda item: (item["created_at"], item["id"])):
        files = []
        for entry in item["files"]:
            status, _headers, data = cli._http(
                "GET", f"/maint/items/{item['id']}/files/{quote(entry['name'], safe='')}", bundle=bundle)
            if status != 200 or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise MaintError(f"{item['id']}/{entry['name']} could not be fetched intact ({status})")
            files.append(changeset.EvidenceFile(f"{item['id']}/{entry['name']}", data, entry.get("origin") or {}))
        packet, errors = changeset.build_packet({"id": item["id"], "item_files": [file.name for file in files]}, files)
        identity = f"{item['topic_key']}@{item['id']}"
        parts = [(identity, packet.filename, packet.data)] if packet and not errors else [
            (f"{identity}#{file.name.split('/', 1)[1]}", file.name.replace("/", "-"), file.data) for file in files]
        for identity, name, data in parts:
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            sources.append({"identity": identity, "path": str(target.resolve()),
                            "sha256": hashlib.sha256(data).hexdigest()})
    _write(path, {"sources": sources})
    return {"manifest": str(path), "items": len(items), "sources": len(sources)}


def _settled(job: dict) -> bool:
    """A P0 ingest attempt that can no longer land: it needed conversion, or failed and rolled back."""
    return job.get("status") == "needs-conversion" or (job.get("status") == "failed"
                                                       and job.get("phase") == "rolled_back")


def _ledger_ingest(bundle: str, entry: dict) -> str | None:
    """``done`` when a P0 ingest of the entry landed, ``in_flight`` when one may still land or
    may have landed unrolled-back, else None. The writer's job decides, not the ledger's last poll."""
    unsettled = entry.get("submitting") == "ingest"  # a lost POST may have created a job
    for job in entry.get("ingest") or []:
        if not isinstance(job, dict) or _settled(job):
            continue
        if job.get("status") != "done" and isinstance(job.get("id"), str):
            status, live = _call("GET", f"/jobs/{quote(job['id'], safe='')}", bundle=bundle, ok=(200, 404))
            job = live if status == 200 else job
        if job.get("status") == "done":
            return "done"
        unsettled = unsettled or not _settled(job)
    return "in_flight" if unsettled else None


def import_ledger(bundle: str, ledger_dir: Path) -> dict:
    """The P0 ledger's unfinished sources as ready items (design §9 day 0).

    A pending or needs_repair source none of whose ingests can land becomes an item under
    ``ledger:<identity>``; versions of one identity fold into one item, the newest bytes on
    top. Text is redacted as collected evidence is, but never clipped, and the ledger's sha256
    stays in the origin. A source whose ingest is done waits for its Codex audit instead
    (``maint begin`` resubmits it), and one whose ingest may still land is left out as
    ``in_flight`` (finish the P0 run first), so no source is curated twice.
    """
    from aiwiki.maint import planner
    from aiwiki.runtime import secrets

    ledger = _read(ledger_dir / "state.json")
    if ledger is None or not isinstance(ledger.get("sources"), list):
        raise MaintError(f"{ledger_dir} holds no ai-wiki maintain state.json", USAGE)
    if ledger.get("bundle") not in (None, bundle):
        raise MaintError(f"the ledger belongs to bundle {ledger['bundle']!r}, not {bundle!r}", USAGE)
    candidates, audit_pending, in_flight, unreadable, redactions = [], [], [], [], 0
    for entry in ledger["sources"]:
        identity, sha = str(entry.get("identity")), entry.get("sha256")
        if entry.get("status") not in ("pending", "needs_repair"):
            continue
        landed = _ledger_ingest(bundle, entry)
        if landed:
            (audit_pending if landed == "done" else in_flight).append(identity)
            continue
        frozen = Path(str(entry.get("path") or ""))
        if not frozen.is_file() and isinstance(sha, str):  # a ledger moved with its state directory
            frozen = ledger_dir / "sources" / sha / frozen.name
        try:
            data = frozen.read_bytes()
        except OSError:
            data = b""
        if not data or hashlib.sha256(data).hexdigest() != sha:
            unreadable.append(identity)
            continue
        suffix = re.sub(r"[^A-Za-z0-9.]", "", frozen.suffix)[:16]
        origin = {"kind": "ledger", "identity": identity[:500], "sha256": sha, "ledger_status": entry["status"]}
        count = 0
        with contextlib.suppress(UnicodeDecodeError):  # binary sources stay verbatim
            text, count = secrets.redact(data.decode("utf-8"))
            data = text.encode()
        redactions += count
        candidates.append({
            "collector": "ledger", "topic_key": f"ledger:{identity}"[:500], "origin": origin,
            "brief": f"P0 ledger source {identity} ({entry['status']})"
                     + (f": {entry['error']}" if entry.get("error") else ""),
            "files": [{"name": f"source{suffix}", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                       "data": data, "origin": {**origin, "kind": "ledger-source", "redactions": count}}],
            "signals": {}})
    enqueued = _enqueue(bundle, None, planner.plan(candidates))
    return {"ledger": str(ledger_dir), "imported": len(candidates), **enqueued, "redactions": redactions,
            "audit_pending": audit_pending, "in_flight": in_flight, "unreadable": unreadable}


# --- the command line ----------------------------------------------------------------------------


def _duration(value: str) -> int:
    match = re.fullmatch(r"(\d+)([smh]?)", value.strip())
    if not match or int(match[1]) == 0:
        raise argparse.ArgumentTypeError("a positive duration such as 100m, 2h or 5400s")
    return int(match[1]) * {"": 60, "s": 1, "m": 60, "h": 3600}[match[2]]


def _only(value: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))
    if not names or set(names) - set(COLLECTORS):
        raise argparse.ArgumentTypeError("a comma-separated subset of " + ",".join(COLLECTORS))
    return names


def _group(value: str) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    if not names:
        raise argparse.ArgumentTypeError("a comma-separated list of the item's file names")
    return names


def add_parser(sub, common: dict) -> None:
    """Register ``ai-wiki maint`` and its verbs on the root parser's subcommands."""
    maint = sub.add_parser(
        "maint", help="the maintainer loop: begin, next, skip/park/split, end; migration verbs",
        command_path="ai-wiki maint", epilog=cli._examples(
            'ai-wiki -b solvely-wiki maint begin --run "$MULTICA_ISSUE_ID"', "ai-wiki maint next --json",
            'ai-wiki maint end --run "$MULTICA_ISSUE_ID" --format md'), **common)
    verbs = maint.add_subparsers(dest="action", required=True)

    def verb(name: str, help_: str, *examples: str) -> argparse.ArgumentParser:
        parser = verbs.add_parser(name, help=help_, command_path=f"ai-wiki maint {name}",
                                  epilog=cli._examples(*examples), **common)
        parser.add_argument("--state-dir", type=Path, default=cli._STATE_DIR, help=f"default: {cli._STATE_DIR}")
        return parser

    begin_ = verb("begin", "doctor, lease, audit resubmission, workspace pull and collect (exit 4 fails closed)",
                  'ai-wiki -b solvely-wiki maint begin --run "$MULTICA_ISSUE_ID"',
                  "ai-wiki maint begin --run WAIO-612 --max-items 3 --only repos")
    begin_.add_argument("--run", required=True, type=cli._run, help="the run id (X-AIWiki-Run)")
    begin_.add_argument("--max-items", type=cli._positive, default=6, help="items this run may take (default: 6)")
    begin_.add_argument("--deadline", type=_duration, default=100 * 60, help="stop taking items after (default: 100m)")
    begin_.add_argument("--dir", type=Path, help="the workspace (default: <state-dir>/runs/<run>/ws)")
    collect_ = verb("collect", "run the collectors, enqueue their evidence, then move the cursors",
                    "ai-wiki maint collect --only issues")
    for parser in (begin_, collect_):
        parser.add_argument("--only", type=_only, default=COLLECTORS, help="collectors (default: repos,issues)")
    next_ = verb("next", "claim the next work item (exit 10 queue empty, 11 budget spent, 12 workspace dirty)",
                 "ai-wiki maint next --json")
    evidence = verb("add-evidence", "freeze a Git file or a Multica issue into the item",
                    "ai-wiki maint add-evidence it_3f2a9c1b7d10 https://code.example/web.git@1a2b3c4:docs/x.md#L10-40",
                    "ai-wiki maint add-evidence it_3f2a9c1b7d10 issue:WAIO-587#01a0c068")
    evidence.add_argument("item")
    evidence.add_argument("ref", help="<remote>@<commit>:<path>[#Lx-y] or issue:<id>[#<comment>]")
    skip = verb("skip", "close the item without a changeset",
                "ai-wiki maint skip it_3f2a9c1b7d10 --reason out_of_scope")
    skip.add_argument("item")
    skip.add_argument("--reason", required=True, help=", ".join(SKIP_REASONS) + " or duplicate_of:<path>")
    skip.add_argument("--note")
    park = verb("park", "hand the item back to the queue; its edits leave the workspace",
                "ai-wiki maint park it_3f2a9c1b7d10 --class model_output --detail 'yaml_parse twice'")
    park.add_argument("item")
    park.add_argument("--class", dest="cls", required=True, choices=PARK_CLASSES)
    park.add_argument("--detail", required=True)
    split = verb("split", "close the item as split; each --group of its files becomes an item",
                 "ai-wiki maint split it_3f2a9c1b7d10 --group S1-README.md,S2-status.md --group S3-notes.md")
    split.add_argument("item")
    split.add_argument("--group", action="append", type=_group, required=True, metavar="FILE[,FILE…]")
    split.add_argument("--reason")
    end_ = verb("end", "release the lease and print the run's report (exit 3: new needs_human)",
                'ai-wiki maint end --run "$MULTICA_ISSUE_ID" --format md', "ai-wiki maint end --run WAIO-612 --json")
    end_.add_argument("--run", required=True, type=cli._run)
    end_.add_argument("--format", choices=("md", "json"), default="md")
    status = verb("status", "the writer's maintenance snapshot", "ai-wiki maint status --json")
    import_v4_ = verb("import-v4", "seed the writer's cursors from a P0 v4 checkpoint (find output too)",
                      "ai-wiki maint import-v4 find.json", "ai-wiki maint import-v4 v4.json --replace")
    import_v4_.add_argument("checkpoint", type=Path)
    import_v4_.add_argument("--replace", action="store_true", help="overwrite cursors the writer already holds")
    import_v4_.add_argument("--run", default="maint:import-v4", type=cli._run, help="run recorded on each cursor")
    export = verb("export-v4", "print the writer's cursors as a v4 checkpoint; optionally unfinished items",
                  "ai-wiki maint export-v4 --output v4.json --pending-manifest sources.json")
    export.add_argument("--output", type=Path, help="write the checkpoint here instead of stdout")
    export.add_argument("--pending-manifest", type=Path, help="write unfinished items as a maintain manifest")
    export.add_argument("--repo-root", help="the checkpoint's repo_root (default: repos.root of the config)")
    export.add_argument("--completed-at", help="RFC 3339 completion time (default: now)")
    ledger = verb("import-ledger", "turn the P0 maintain ledger's unfinished sources into work items",
                  "ai-wiki maint import-ledger --ledger ~/.ai-wiki/maintenance/solvely-wiki")
    ledger.add_argument("--ledger", type=Path, required=True, help="the maintain --state-dir holding state.json")
    for parser in (begin_, collect_, evidence, export):
        parser.add_argument("--config", type=Path, default=CONFIG, help=f"collector settings (default: {CONFIG})")
    for parser in (begin_, collect_, next_, evidence, skip, park, split, end_, status, import_v4_, export, ledger):
        parser.add_argument("--json", action="store_true", help="emit JSON instead of TOON")


def _bundle(selected: str | None) -> str:
    """A concrete bundle name: ``-b``, the active bundle, else the server's default."""
    name = selected or cli._active() or _call("GET", "/health", bundle=None)[1].get("bundle")
    if not name:
        raise MaintError("no bundle selected: pass -b <bundle> or run ai-wiki bundle use <name>", USAGE)
    return name


def _print(result: dict, as_json: bool, name: str) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    scalars = {key: value for key, value in result.items() if not isinstance(value, (dict, list))}
    nested = [object_lines(key, value) for key, value in result.items() if isinstance(value, dict)]
    lists = [table_lines(key, value, sorted({field for row in value for field in row}))
             if value and all(isinstance(row, dict) for row in value) else object_lines(None, {key: value})
             for key, value in result.items() if isinstance(value, list)]
    emit(object_lines(name, scalars), *nested, *lists)


def command(a: argparse.Namespace, selected: str | None) -> int:
    from aiwiki.cli import workspace

    state_dir = Path(a.state_dir).expanduser().resolve()
    as_json = getattr(a, "json", False)
    try:
        if a.action == "begin":
            code, result = begin(_bundle(selected), run=a.run, state_dir=state_dir, config=a.config.expanduser(),
                                 max_items=a.max_items, deadline_s=a.deadline, only=a.only, workspace_dir=a.dir)
        elif a.action == "collect":
            current = _current(state_dir, selected)
            bundle = _bundle(selected or current.get("bundle"))
            run = current.get("run") if current.get("bundle") == bundle else None
            folder = run_dir(state_dir, run) if run else state_dir / "collect"
            results = collect(bundle, run=run, work=folder / "collect", state_dir=state_dir,
                              config=_config(a.config.expanduser()), only=a.only)
            if run:  # the run's report counts it
                state = load_run(state_dir, run)
                state["collect"] = _merged(state.get("collect") or {}, results)
                _save_run(state_dir, state)
            code, result = _collect_code(results), {"bundle": bundle, "run": run, "collect": results}
        elif a.action == "next":
            code, result = next_item(state_dir, selected)
        elif a.action == "add-evidence":
            code, result = add_evidence(state_dir, selected, a.item, a.ref, a.config.expanduser())
        elif a.action == "skip":
            duplicate = a.reason.startswith("duplicate_of:")
            if not duplicate and a.reason not in SKIP_REASONS:
                raise MaintError("--reason is one of " + ", ".join(SKIP_REASONS) + " or duplicate_of:<path>", USAGE)
            code, result = resolve(state_dir, selected, a.item, {
                "outcome": "duplicate" if duplicate else "skipped", "reason": a.reason,
                **({"note": a.note} if a.note else {})})
        elif a.action == "park":
            code, result = resolve(state_dir, selected, a.item,
                                   {"outcome": "parked", "class": a.cls, "reason": a.detail})
        elif a.action == "split":
            code, result = resolve(state_dir, selected, a.item, {
                "outcome": "split", "children": [{"files": group} for group in a.group],
                **({"reason": a.reason} if a.reason else {})})
        elif a.action == "end":
            code, result, markdown = end(state_dir, selected, a.run)
            if a.json or a.format == "json":
                print(json.dumps({**result, "report": markdown}, ensure_ascii=False, indent=2))
            else:
                print(markdown, end="")
            return code
        elif a.action == "status":
            result = _call("GET", "/maint/status", bundle=_bundle(selected))[1]
            if as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                emit(*_status_lines(result))
            return OK
        elif a.action == "import-v4":
            return import_v4(_bundle(selected), a.checkpoint.expanduser(), replace=a.replace, run=a.run,
                             as_json=as_json)
        elif a.action == "export-v4":
            bundle = _bundle(selected)
            found = export_v4(bundle, completed_at=a.completed_at,
                              repo_root=a.repo_root or (_config(a.config.expanduser()).get("repos") or {}).get("root"))
            text = json.dumps(found, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            manifest = export_manifest(bundle, a.pending_manifest.expanduser()) if a.pending_manifest else None
            if a.output:
                a.output.expanduser().write_text(text, encoding="utf-8")
                _print({"checkpoint": str(a.output), **({"manifest": manifest} if manifest else {})}, as_json,
                       "export")
            else:
                print(text, end="")
            return OK
        else:  # import-ledger
            code, result = OK, import_ledger(_bundle(selected), a.ledger.expanduser())
    except (MaintError, workspace.WorkspaceError, OSError) as exc:  # OSError: the writer or a file unreachable
        code = FAILED if isinstance(exc, OSError) else exc.code
        if as_json:
            print(json.dumps({"error": str(exc), "status": getattr(exc, "status", None),
                              "detail": getattr(exc, "detail", None)}, ensure_ascii=False))
            return code
        cli._fail(str(exc), help_command=f"ai-wiki maint {a.action} --help", code=code)
    _print(result, as_json, a.action.replace("-", "_"))
    return code
