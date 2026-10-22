"""The agent skills ship inside the ai-wiki package and are installed from it.

    packaged  ``aiwiki/skills/<name>/`` in a wheel (pyproject force-includes the repository's ``skills/``),
              ``skills/<name>/`` in a checkout. Each SKILL.md carries ``<!-- ai-wiki:version X.Y.Z -->`` right after
              its frontmatter; ``tests/test_skills_ship.py`` fails when a stamp is not ``VERSION``.
    target    ``--dir``, else ``$AIWIKI_SKILLS_HOME``, else ``~/.agents/shared-skills`` when the host has it (a Multica
              daemon host keeps skill bodies there so the daemon does not scan them twice), else ``~/.agents/skills``.
              Every command resolves the same directory, so ``ai-wiki update`` never installs a second copy.
              A Multica-managed ``multica-metadata.json`` there is kept; every other file comes from the package,
              so a deleted resource cannot linger. A directory whose SKILL.md names another skill is never touched.
    status    by content, not by stamp: ``main`` moves without a version bump, so equal stamps can still differ.
    update    ``ai-wiki update`` installs ``main``'s head with uv, then runs the *new* code's ``ai-wiki skill install``,
              so a skill can never lag the CLI that installed it.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from aiwiki.version import VERSION

NAMES = ("ai-wiki", "ai-wiki-curating-maintainer", "ai-wiki-auditor", "okf-knowledge-curator")
# Retired skills: ``skill install`` removes an installed copy whose SKILL.md still names it, so a runtime
# never keeps loading a flow this release no longer ships (ai-wiki-maintainer: the legacy ledger flow, 0.3.3).
RETIRED = ("ai-wiki-maintainer",)
PRESERVE = {"multica-metadata.json"}
_IGNORED = PRESERVE | {".DS_Store"}
_STAMP = re.compile(r"<!-- ai-wiki:version (\d+(?:\.\d+)*) -->")


class SkillError(Exception):
    """A refusal with a code the CLI maps to its exit status (2: the command is wrong)."""

    def __init__(self, message: str, *, code: int = 1, help_command: str | None = None):
        super().__init__(message)
        self.code = code
        self.help_command = help_command


def packaged_root() -> Path:
    """Where this installation's skills are: inside the wheel, or the checkout's ``skills/``."""
    here = Path(__file__).resolve().parent
    for root in (here / "skills", here.parents[1] / "skills"):
        if (root / "ai-wiki" / "SKILL.md").is_file():
            return root
    raise SkillError("this ai-wiki carries no skills (a broken install); reinstall with `ai-wiki update`")


def default_dir() -> Path:
    """Where this host's agent skills live (same rule as the control repo's sync-skill-mirror.sh)."""
    if os.environ.get("AIWIKI_SKILLS_HOME"):
        return Path(os.environ["AIWIKI_SKILLS_HOME"]).expanduser()
    shared = Path.home() / ".agents" / "shared-skills"
    return shared if shared.is_dir() else Path.home() / ".agents" / "skills"


def stamp(text: str) -> str | None:
    match = _STAMP.search(text)
    return match.group(1) if match else None


def skill_name(text: str) -> str | None:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    name = match and re.search(r"(?m)^name:\s*(.*?)\s*$", match.group(1))
    return name.group(1).strip("'\"") if name else None


def files(root: Path) -> dict[str, str]:
    """{relative path: sha256} of a skill directory, ignoring platform-managed files and caches."""
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in _IGNORED or "__pycache__" in path.parts:
            continue
        out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _foreign(name: str, target: Path) -> str | None:
    """Why ``target`` is not ours to replace, else None."""
    if target.is_symlink():
        return None  # an old checkout link: install replaces the link, never what it points to
    if not target.exists():
        return None
    if not target.is_dir():
        return "a file, not a directory"
    text = _read(target / "SKILL.md")
    if text is None:
        leftovers = [p.name for p in target.iterdir() if p.name not in _IGNORED]
        return None if not leftovers else "a directory without SKILL.md"
    found = skill_name(text)
    return None if found == name else f"its SKILL.md is name: {found}" if found else "its SKILL.md has no name"


def status(dest: Path | None = None) -> list[dict]:
    """One row per skill: current | stale | missing | foreign, with the installed stamp."""
    source, dest = packaged_root(), (dest or default_dir())
    rows = []
    for name in NAMES:
        target = dest / name
        want = files(source / name)
        why = _foreign(name, target)
        if why:
            state = "foreign"
        elif not (target / "SKILL.md").is_file():
            state = "missing"
        elif target.is_symlink():
            state = "stale"  # a link into some checkout: not what this ai-wiki ships
        else:
            state = "current" if files(target) == want else "stale"
        rows.append({"skill": name, "status": state, "installed": stamp(_read(target / "SKILL.md") or ""),
                     "packaged": stamp(_read(source / name / "SKILL.md") or ""), "path": str(target),
                     **({"detail": why} if why else {})})
    return rows


def _replace(source: Path, target: Path) -> None:
    """Swap ``target`` for a copy of ``source``, keeping its platform-managed files; never half a directory."""
    preserved = {} if target.is_symlink() else {
        name: (target / name).read_bytes() for name in PRESERVE if (target / name).is_file()}
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent))
    try:
        shutil.copytree(source, staging / "new", ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
        for name, content in preserved.items():
            (staging / "new" / name).write_bytes(content)
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            target.rename(staging / "old")
        (staging / "new").rename(target)
    except BaseException:
        if not target.exists() and (staging / "old").exists():
            (staging / "old").rename(target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install(dest: Path | None = None, names: tuple[str, ...] = NAMES) -> list[dict]:
    """Install the packaged skills into ``dest``; refuses (changing nothing) when any target is not ai-wiki's."""
    source, dest = packaged_root(), (dest or default_dir())
    unknown = [n for n in names if n not in NAMES]
    if unknown:
        raise SkillError(f"unknown skill(s): {', '.join(unknown)} (choose from {', '.join(NAMES)})", code=2,
                         help_command="ai-wiki skill --help")
    for name in names:
        version = stamp(_read(source / name / "SKILL.md") or "")
        if version != VERSION:
            raise SkillError(f"the packaged {name} skill is stamped {version}, not {VERSION}: nothing was written; "
                             "reinstall ai-wiki")
    blocked = [(n, _foreign(n, dest / n)) for n in names if _foreign(n, dest / n)]
    if blocked:
        detail = "; ".join(f"{dest / n} is not ai-wiki's ({why})" for n, why in blocked)
        raise SkillError(f"{detail}: nothing was written; move it away or pass another --dir", code=2)
    dest.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in RETIRED if names == NAMES else ():
        target = dest / name
        if target.is_symlink():
            target.unlink()
        elif target.is_dir() and skill_name(_read(target / "SKILL.md") or "") == name:
            shutil.rmtree(target)  # only our own retired skill; a foreign directory of that name stays
        else:
            continue
        rows.append({"skill": name, "status": "removed", "path": str(target)})
    for name in names:
        target = dest / name
        before = None if target.is_symlink() else files(target) or None
        if before == files(source / name) and not target.is_symlink():
            rows.append({"skill": name, "status": "unchanged", "path": str(target)})
            continue
        _replace(source / name, target)
        rows.append({"skill": name, "status": "installed" if before is None else "updated", "path": str(target)})
    return rows
