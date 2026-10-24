"""``ai-wiki update [--check]`` and ``ai-wiki skill [install]``: the CLI and its skills move together.

``update`` reads ``main``'s head from the repository, installs exactly that commit with ``uv tool install --force``,
then runs the newly installed ``ai-wiki skill install`` (the new code installs its own skills). When the CLI is
already at ``main`` it still repairs stale skills. A failed skill step never undoes the CLI step; it is reported.

``update`` never replaces the CLI under a live run on this host: it refuses (exit 1, ``run_live``) while a
``maint`` or ``review`` run of the state directory was active within the writer's lease TTL, or a legacy
``maintain`` holds its ``runner.lock``, and installs under an exclusive ``update.lock`` (a second updater
refuses with ``update_running``). ``--check`` reports such a run and then offers no update.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from aiwiki import skills
from aiwiki.cli.toon import emit, object_lines, table_lines
from aiwiki.version import VERSION

REPO = os.environ.get("AIWIKI_UPDATE_REPO", "https://github.com/Scorpion1221/ai-wiki")
BRANCH = "main"
LATER = "before your next run (never during one)"
LIVE_FIELDS = ("kind", "bundle", "run", "age_minutes")


def _held(path: Path) -> bool:
    """Whether another open file holds an flock on ``path``: a non-blocking probe that never creates it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _live(state_dirs: list[Path]) -> list[dict]:
    """The runs on this host an update must not replace the CLI under.

    A ``maint``/``review`` pointer counts while its run wrote locally within the writer's lease TTL (every verb
    of a run saves its record, as every call renews the lease); a run that died without ``end`` stops counting
    when its lease would have expired. A legacy ``maintain`` counts while it holds ``runner.lock``.
    """
    from aiwiki.cli import maint
    from aiwiki.service.maint_state import LEASE_TTL

    now, rows = time.time(), []
    for state_dir in state_dirs:
        for kind, folder, record in (("maint", "runs", "run.json"), ("review", "reviews", "review.json")):
            for pointer in sorted((state_dir / folder).glob("current-*.json")):
                current = maint._read(pointer) or {}
                touched = [pointer]
                if isinstance(current.get("run"), str):
                    run_dir = state_dir / folder / maint._slug(current["run"])
                    touched += [run_dir / record, run_dir / "proposals.jsonl"]
                age = now - max(_mtime(path) for path in touched)
                if age < LEASE_TTL.total_seconds():
                    rows.append({"kind": kind, "bundle": current.get("bundle"), "run": current.get("run"),
                                 "age_minutes": max(int(age // 60), 0)})
        if _held(state_dir / "runner.lock"):
            ledger = maint._read(state_dir / "state.json") or {}
            rows.append({"kind": "maintain", "bundle": ledger.get("bundle"), "run": None, "age_minutes": None})
    return rows


def _refuse(code: str, message: str, live: list[dict]) -> int:
    emit(object_lines("error", {"kind": "error", "code": code, "message": message}),
         table_lines("live_runs", live, LIVE_FIELDS) if live else [])
    return 1


def _with_skill_md(rows: list[dict]) -> list[dict]:
    return [{**row, "skill_md": str(Path(row["path"]) / "SKILL.md")} for row in rows]


def _receipt() -> dict:
    """The installed commit, from uv's receipt (``git = "<repo>?rev=…#<sha>"``) or the package's direct_url.json."""
    try:
        from importlib import metadata

        dist = metadata.distribution("ai-wiki")
        direct = dist.read_text("direct_url.json")
        if direct:
            data = json.loads(direct)
            commit = (data.get("vcs_info") or {}).get("commit_id")
            if commit:
                return {"commit": commit, "url": data.get("url")}
            if (data.get("dir_info") or {}).get("editable"):
                return {"commit": None, "editable": True}
    except Exception:  # noqa: BLE001 — a probe: an unknown commit is reported, not fatal
        pass
    return {"commit": None}


def _latest() -> str:
    try:
        out = subprocess.run(["git", "ls-remote", REPO, f"refs/heads/{BRANCH}"], capture_output=True, text=True,
                             timeout=60, check=True, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise skills.SkillError(f"cannot read {BRANCH} from {REPO}: {exc}") from None
    sha = out.split()[0] if out.split() else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise skills.SkillError(f"{REPO} has no {BRANCH} branch")
    return sha


def _summary(rows: list[dict]) -> str:
    states = {row["status"] for row in rows}
    return "current" if states == {"current"} else "foreign" if "foreign" in states else "stale"


def skill_command(args) -> int:
    dest = Path(args.dir).expanduser() if args.dir else None
    if args.action == "install":
        rows = skills.install(dest, tuple(args.names) or skills.NAMES)
        emit(object_lines("skills", {"version": VERSION, "dir": str(dest or skills.default_dir())}),
             table_lines("installed", _with_skill_md(rows), ("skill", "status", "skill_md")))
        return 0
    rows = skills.status(dest)
    overall = _summary(rows)
    where = str(dest or skills.default_dir())
    groups = [object_lines("skills", {"status": overall, "version": VERSION, "dir": where}),
              table_lines("skills", _with_skill_md(rows), ("skill", "status", "installed", "packaged", "skill_md"))]
    if overall == "stale":
        install = "ai-wiki skill install" + (f" --dir {dest}" if dest else "")
        groups.append(table_lines("help", ({"command": install, "purpose": "install the skills this ai-wiki ships"},),
                                  ("command", "purpose")))
    elif overall == "foreign":
        groups.append(table_lines("help", ({"command": "ai-wiki skill --dir <your skills dir>",
                                            "purpose": "a path holds another skill: move it, or name another dir"},),
                                  ("command", "purpose")))
    emit(*groups)
    return 0


def _default_state_dir() -> Path:
    """The state directory ``maint`` and ``review`` default to."""
    from aiwiki.cli import main as cli

    return cli._STATE_DIR.expanduser()


def update_command(args) -> int:
    # The default state directory always; ``--state-dir`` adds others, such as a legacy ``maintain`` ledger.
    dirs = list(dict.fromkeys([_default_state_dir(), *(Path(d).expanduser() for d in args.state_dir or ())]))
    lock_path = dirs[0] / "update.lock"
    if args.check:
        return _check(dirs, lock_path)
    dirs[0].mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _refuse("update_running", f"another `ai-wiki update` holds {lock_path}; nothing was installed. "
                           "Go on with the installed version and update " + LATER, [])
        live = _live(dirs)
        if live:
            return _refuse("run_live", "a run is live on this host, so the CLI and skills under it stay as they are; "
                           "nothing was installed. Go on with the installed version and update " + LATER, live)
        return _install()
    finally:
        os.close(fd)  # releases the flock


def _status() -> tuple[dict, str, list[dict]]:
    installed = _receipt()
    latest = _latest()
    rows = skills.status()
    data = {"installed": f"{VERSION} ({(installed.get('commit') or 'unknown')[:12]})", "latest": latest[:12],
            "available": installed.get("commit") != latest, "skills": _summary(rows)}
    if installed.get("editable"):
        data["available"] = False
        data["note"] = "an editable checkout install: update it with git, then `ai-wiki skill install`"
    return data, latest, rows


def _check(dirs: list[Path], lock_path: Path) -> int:
    data, _latest_sha, rows = _status()
    live = _live(dirs) + ([{"kind": "update", "bundle": None, "run": None, "age_minutes": None}]
                          if _held(lock_path) else [])
    if live:
        data["wait"] = "a run or an update is live on this host: update " + LATER
    groups = [object_lines("update", data),
              table_lines("skills", _with_skill_md(rows), ("skill", "status", "skill_md"))]
    if live:
        groups.append(table_lines("live_runs", live, LIVE_FIELDS))  # and no help row: nothing to run now
    elif data["available"] or data["skills"] != "current":
        groups.append(table_lines("help", ({"command": "ai-wiki update",
                                            "purpose": "install the latest CLI and its skills together, " + LATER},),
                                  ("command", "purpose")))
    emit(*groups)
    return 0


def _install() -> int:
    data, latest, _rows = _status()
    if data["available"]:
        uv = shutil.which("uv")
        if not uv:
            raise skills.SkillError("uv is not on PATH; install uv, then re-run `ai-wiki update`")
        spec = f"git+{REPO}@{latest}"
        print(f"ai-wiki: installing {spec}", file=sys.stderr)
        done = subprocess.run([uv, "tool", "install", "--force", spec], stdin=subprocess.DEVNULL, check=False,
                              stdout=sys.stderr, stderr=sys.stderr, timeout=900)
        if done.returncode:
            raise skills.SkillError(f"uv tool install failed (exit {done.returncode}); ai-wiki {VERSION} is unchanged")
    # The skills come from the ai-wiki now on PATH: after an upgrade that is the new code, not this process.
    exe = shutil.which("ai-wiki")
    if not exe:
        raise skills.SkillError("ai-wiki is not on PATH after the install; add uv's tool bin dir (`uv tool dir --bin`)")
    step = subprocess.run([exe, "skill", "install"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          check=False, timeout=300)
    after = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False, timeout=60).stdout.strip()
    data = {**data, "status": "updated" if data["available"] else "already latest", "now": after}
    if step.returncode:
        data["skills"] = "not installed"
        emit(object_lines("update", data), step.stdout.strip().splitlines())
        return step.returncode
    data["skills"] = "current"
    # The new code's own `skill install` table: where each installed SKILL.md now is.
    groups = [object_lines("update", data), step.stdout.strip().splitlines()]
    if data["available"]:
        groups.append(table_lines("help", ({
            "command": "compare the <!-- ai-wiki:version --> of the Skill you follow with `now`",
            "purpose": "if they differ, read the installed SKILL.md listed above; if you cannot, stop blocked: "
                       "workspace Skill is behind the CLI; publish it"},), ("command", "purpose")))
    emit(*groups)
    return 0
