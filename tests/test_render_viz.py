from __future__ import annotations

from pathlib import Path

from aiwiki.engine.render_viz import generate_visualization


def test_generate_visualization_uses_packaged_viewer_assets(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "concept.md").write_text(
        """---
type: Feature
title: Demo concept
description: Render me
tags: [demo]
status: draft
confidence: low
source_ref: source.md
---

# Demo

Viewer body.
""",
        encoding="utf-8",
    )
    output = tmp_path / "viewer.html"

    stats = generate_visualization(bundle, output, bundle_name="Demo KB")
    html = output.read_text(encoding="utf-8")

    assert stats["concepts"] == 1
    assert 'window.BUNDLE_NAME = "Demo KB";' in html
    assert '"label": "Demo concept"' in html
    assert "Knowledge Observatory" in html
    assert "const bundle = window.BUNDLE" in html
    assert "__BUNDLE_" not in html and "__VIZ_" not in html
