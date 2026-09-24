"""``ai-wiki doctor --role``: fail closed before a run whose runtime cannot do its job (design §3).

Checks that an endpoint and token are configured, the writer's API contract (``/whoami``),
that the token's scopes are exactly the role's and, for a curator or auditor, that it has an
actor to stamp and that the writer rather than a read mirror answered, the bundle's OKF
version, a writable state directory with 2 GB free, the tools the role shells out to, and,
with ``--skills-dir``, that the role's skills are installed (each reported by its digest, so
a run records which skill version it ran).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from aiwiki.cli import main as cli
from aiwiki.service.auth import ROLES
from aiwiki.version import VERSION

MIN_FREE_BYTES = 2 * 1024 ** 3
TOOLS = {"curator": ("git", "uv", "multica"), "auditor": ("git", "uv"), "member": ()}
SKILLS = {"curator": ("ai-wiki", "ai-wiki-curating-maintainer", "okf-knowledge-curator"),
          "auditor": ("ai-wiki",), "member": ("ai-wiki",)}
_IGNORED = {"multica-metadata.json", ".DS_Store"}  # as scripts/sync_skills.py


def _version(value: object) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", str(value))[:3])


def _get(route: str, bundle: str | None = None) -> tuple[int | None, dict]:
    try:
        status, _headers, body = cli._http("GET", route, bundle=bundle)
    except OSError as exc:
        return None, {"detail": f"network: {exc}"}
    try:
        value = json.loads(body)
    except ValueError:
        value = None
    return status, value if isinstance(value, dict) else {}


def skill_digest(root: Path) -> str:
    """One sha256 over a skill's files (path and content), ignoring platform-managed files."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in _IGNORED and "__pycache__" not in path.parts:
            digest.update(f"{path.relative_to(root).as_posix()}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n"
                          .encode())
    return digest.hexdigest()


def _server(role: str, bundle: str | None, check) -> None:
    status, who = _get("/whoami")
    if status != 200:
        detail = who.get("detail")
        check("whoami", False, f"GET /whoami answered {status}" + (f": {detail}" if detail else ""))
    else:
        api, minimum = (who.get("api") or {}).get("changesets"), (who.get("client") or {}).get("min")
        check("api", api == 1 and _version(VERSION) >= _version(minimum),
              f"api.changesets={api}; client {VERSION}, writer needs >= {minimum}")
        scopes, wanted = set(who.get("scopes") or []), ROLES[role]
        missing, extra = sorted(wanted - scopes), sorted(scopes - wanted)
        detail = f"{who.get('principal')} holds {', '.join(sorted(scopes)) or 'nothing'}"
        for label, names in (("missing", missing), ("extra", extra)):
            detail += f"; {label} {', '.join(names)}" if names else ""
        check("scopes", not missing and not extra, detail)
        if role != "member":  # the writer refuses a changeset it cannot stamp generated.by / verified.by for
            check("actor", bool(who.get("actor")), f"{who.get('principal')} stamps as {who.get('actor')}"
                  if who.get("actor") else f"{who.get('principal')} has no actor; it cannot propose")
            # The public /health comes from the read mirror; a run needs the writer's answers.
            check("writer", who.get("writer") is True, "the writer answered" if who.get("writer") is True
                  else "a read mirror answered /whoami; route it to the writer (design §2.1)")
    status, health = _get("/health", bundle)
    check("okf_version", status == 200 and health.get("okf_version") == "0.2",
          f"bundle {health.get('bundle')} okf_version {health.get('okf_version')}" if status == 200
          else f"GET /health answered {status}")


def run(role: str, *, bundle: str | None, state_dir: Path, skills_dir: Path | None) -> dict:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    config = cli._normalize(cli._load())
    if not config.get("endpoint") or not cli._token(config):
        check("config", False, "no endpoint or token; run: ai-wiki config set --endpoint <url> --token <token>")
    else:
        _server(role, bundle, check)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=state_dir):
            pass
        free = shutil.disk_usage(state_dir).free
    except OSError as exc:
        check("state_dir", False, f"{state_dir}: {exc.strerror or exc}")
    else:
        check("state_dir", True, str(state_dir))
        check("disk", free > MIN_FREE_BYTES, f"{free / 1024 ** 3:.1f} GB free at {state_dir}")
    for tool in TOOLS[role]:
        found = shutil.which(tool)
        check(f"tool:{tool}", found is not None, found or "not on PATH")
    for name in SKILLS[role] if skills_dir else ():
        root = skills_dir / name
        check(f"skill:{name}", (root / "SKILL.md").is_file(),
              f"sha256 {skill_digest(root)}" if (root / "SKILL.md").is_file() else f"{root} has no SKILL.md")
    return {"role": role, "ok": all(row["ok"] for row in checks), "checks": checks}
