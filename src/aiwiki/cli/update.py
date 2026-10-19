"""``ai-wiki update [--check]`` and ``ai-wiki skill [install]``: the CLI and its skills move together.

``update`` reads ``main``'s head from the repository, installs exactly that commit with ``uv tool install --force``,
then runs the newly installed ``ai-wiki skill install`` (the new code installs its own skills). When the CLI is
already at ``main`` it still repairs stale skills. A failed skill step never undoes the CLI step; it is reported.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from aiwiki import skills
from aiwiki.cli.toon import emit, object_lines, table_lines
from aiwiki.version import VERSION

REPO = os.environ.get("AIWIKI_UPDATE_REPO", "https://github.com/Scorpion1221/ai-wiki")
BRANCH = "main"
CACHE = Path(os.environ.get("AIWIKI_CACHE_DIR") or Path.home() / ".cache" / "ai-wiki") / "latest.json"


def _remember(installed: str | None, latest: str) -> None:
    """What this check saw, for the home view (which never touches the network)."""
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"installed": installed, "latest": latest}))
    except OSError:
        pass


def cached_notice() -> str | None:
    """'update available' when the last check saw main ahead of the commit installed now, else None."""
    try:
        seen = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(seen, dict) or not isinstance(seen.get("latest"), str):
        return None
    installed = _receipt().get("commit")
    if not installed or installed != seen.get("installed") or seen["latest"] == installed:
        return None      # updated since the check, or nothing newer: never a stale nag
    return f"main {seen['latest'][:12]} is newer than this install ({installed[:12]})"


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
             table_lines("installed", rows, ("skill", "status", "path")))
        return 0
    rows = skills.status(dest)
    overall = _summary(rows)
    where = str(dest or skills.default_dir())
    groups = [object_lines("skills", {"status": overall, "version": VERSION, "dir": where}),
              table_lines("skills", rows, ("skill", "status", "installed", "packaged"))]
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


def update_command(args) -> int:
    installed = _receipt()
    latest = _latest()
    _remember(installed.get("commit"), latest)
    rows = skills.status()
    data = {"installed": f"{VERSION} ({(installed.get('commit') or 'unknown')[:12]})", "latest": latest[:12],
            "available": installed.get("commit") != latest, "skills": _summary(rows)}
    if installed.get("editable"):
        data["available"] = False
        data["note"] = "an editable checkout install: update it with git, then `ai-wiki skill install`"
    if args.check:
        groups = [object_lines("update", data)]
        if data["available"] or data["skills"] != "current":
            groups.append(table_lines("help", ({"command": "ai-wiki update",
                                                "purpose": "install the latest CLI and its skills together"},),
                                      ("command", "purpose")))
        emit(*groups)
        return 0
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
    groups = [object_lines("update", data)]
    if data["available"]:
        groups.append(table_lines("help", ({"command": "re-read the ai-wiki skill you are following",
                                            "purpose": "it changed with the CLI"},), ("command", "purpose")))
    emit(*groups)
    return 0
