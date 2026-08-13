from pathlib import Path

from aiwiki.engine.lint import lint


def test_lint_uses_v02_status_freshness_and_revision_verification(tmp_path: Path):
    (tmp_path / "SCHEMA.md").write_text("---\ntype: Contract\n---\n# Schema\n", encoding="utf-8")
    (tmp_path / "purpose.md").write_text("---\ntype: Contract\n---\n# Purpose\n", encoding="utf-8")
    (tmp_path / "log.md").write_text("# Log\n\n## 2026-08-13\n* **Creation**: fixture\n", encoding="utf-8")
    concepts = tmp_path / "features"
    concepts.mkdir()
    concept = concepts / "x.md"
    concept.write_text(
        """---
type: Feature
title: X
description: X.
tags: [x]
status: stable
generated: {by: process:test, at: '2026-08-13T02:00:00Z'}
verified: {by: process:audit, at: '2026-08-13T01:00:00Z'}
stale_after: 2020-01-01
sources:
  - {id: x, resource: /sources/x.txt}
---
# Summary
X.
""",
        encoding="utf-8",
    )
    (concepts / "index.md").write_text("# Feature\n\n* [X](x.md) - X.\n", encoding="utf-8")

    findings, count = lint(tmp_path)

    assert count == 1
    assert not [f for f in findings if f["check"] == "frontmatter"]
    assert any(f["check"] == "stale" for f in findings)
    assert any(f["check"] == "verification-lag" for f in findings)
