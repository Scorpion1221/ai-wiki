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


def test_lint_flags_a_concept_past_the_ninety_day_default(tmp_path: Path):
    (tmp_path / "SCHEMA.md").write_text("---\ntype: Contract\n---\n# Schema\n", encoding="utf-8")
    (tmp_path / "purpose.md").write_text("---\ntype: Contract\n---\n# Purpose\n", encoding="utf-8")
    concepts = tmp_path / "features"
    concepts.mkdir()
    for name, at, stale_after in (("old", "2020-01-01T00:00:00Z", ""), ("kept", "2020-01-01T00:00:00Z",
                                                                          "stale_after: 2099-01-01\n")):
        (concepts / f"{name}.md").write_text(
            f"---\ntype: Feature\ntitle: {name}\ndescription: X.\ntags: [x]\nstatus: stable\n"
            f"generated: {{by: process:test, at: '{at}'}}\n{stale_after}"
            "sources:\n  - {id: x, resource: /sources/x.txt}\n---\n# Summary\nX.\n", encoding="utf-8")

    findings, _count = lint(tmp_path)

    stale = [f for f in findings if f["check"] == "stale"]
    assert [(f["where"], f["detail"]) for f in stale] == [
        ("features/old.md", "stale since 2020-03-31 (the 90-day default from generated.at)")]
