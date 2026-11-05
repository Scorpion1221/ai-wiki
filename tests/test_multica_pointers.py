"""The Multica workspace copies are pointers generated from the skills' frontmatter (scripts/multica_pointers.py)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from aiwiki import skills

ROOT = Path(__file__).resolve().parents[1]


def test_pointers_are_current_and_carry_each_skills_trigger() -> None:
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "multica_pointers.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for name in skills.NAMES:
        full = (ROOT / "skills" / name / "SKILL.md").read_text("utf-8")
        pointer = (ROOT / "multica" / name / "SKILL.md").read_text("utf-8")
        assert pointer.startswith(full[: full.index("\n---\n", 4) + 5]), f"{name}: same frontmatter as the skill"
        # Version-free: a release must not change the pointer, or every release would need a republish.
        assert "ai-wiki:version" not in pointer
        assert "`ai-wiki update --check`" in pointer and "`ai-wiki update && hash -r`" in pointer
        assert "`ai-wiki skill`" in pointer and "skill_md" in pointer and "never retry" in pointer
        assert "--token" not in pointer
