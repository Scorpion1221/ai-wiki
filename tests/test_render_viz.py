from __future__ import annotations

from pathlib import Path

import yaml

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
status: stable
generated: {by: process:test, at: 2026-08-13T10:00:00Z}
verified:
  - {by: process:nightly-audit, at: 2026-08-13T11:00:00Z}
stale_after: 2099-01-01
sources:
  - id: demo-source
    resource: https://example.com/source
    title: Demo source
    author: team:demo
---

# Demo

Viewer body.
See [target](/topics/target.md).
""",
        encoding="utf-8",
    )
    (bundle / "topics").mkdir()
    for name in ("target", "log-history"):
        (bundle / "topics" / f"{name}.md").write_text(
            f"""---
type: Reference
title: {name}
description: Fixture reference.
tags: [fixture]
status: draft
generated: {{by: process:test, at: '2026-08-13T00:00:00Z'}}
sources:
  - {{id: fixture, resource: /sources/fixture.md.source}}
---

# {name}
""",
            encoding="utf-8",
        )
    (bundle / "index.md").write_text(
        '---\nokf_version: "0.2"\n---\n\n# Demo\n', encoding="utf-8")
    output = tmp_path / "viewer.html"

    stats = generate_visualization(bundle, output, bundle_name="Demo KB")
    html = output.read_text(encoding="utf-8")

    assert stats["concepts"] == 3
    assert stats["edges"] == 1
    assert 'window.BUNDLE_NAME = "Demo KB";' in html
    assert '"label": "Demo concept"' in html
    assert '"okf_version": "0.2"' in html
    assert '"trust": "machine-confirmed"' in html
    assert '"freshness": "fresh"' in html
    assert '"generated_at": "2026-08-13T10:00:00+00:00"' in html
    assert '"verified_at": "2026-08-13T11:00:00+00:00"' in html
    assert '"verification_current": true' in html
    assert '"current_verified_at": "2026-08-13T11:00:00+00:00"' in html
    assert '"id": "demo-source"' in html
    assert '"id": "topics/log-history"' in html
    assert '"source": "concept", "target": "topics/target"' in html
    assert "detail-sources" in html
    assert "Knowledge Observatory" in html
    assert "const bundle = window.BUNDLE" in html
    assert "__BUNDLE_" not in html and "__VIZ_" not in html


def test_visualization_marks_historical_human_review_as_not_current(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "historical.md").write_text(
        """---
type: Decision
title: Historical review
description: Edited after its human verification.
tags: [trust]
status: stable
generated: {by: process:editor, at: '2026-08-13T12:00:00Z'}
verified:
  - {by: human:owner, at: '2026-08-13T11:00:00Z'}
sources:
  - {id: decision-source, resource: /sources/decision.md.source}
---

# Decision

Current prose was edited after review.
""",
        encoding="utf-8",
    )
    (bundle / "index.md").write_text(
        '---\nokf_version: "0.2"\n---\n', encoding="utf-8"
    )
    output = tmp_path / "viewer.html"

    stats = generate_visualization(bundle, output)
    html = output.read_text(encoding="utf-8")

    assert stats["concepts"] == 1
    assert '"trust": "human-reviewed"' in html
    assert '"verification_current": false' in html
    assert '"current_verified_by": []' in html
    assert '"current_verified_at": ""' in html
    assert '"verified_at": "2026-08-13T11:00:00Z"' in html
    assert "human-reviewed} · historical" not in html
    assert "`${data.trust} · historical`" in html
    assert "none for current revision" in html
    assert "Current verification" in html and "Verification history" in html


def test_visualization_skips_invalid_legacy_and_case_variant_git_docs(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    valid = """---
type: Feature
title: Valid
description: Valid strict-profile concept.
tags: [valid]
status: draft
generated: {by: process:test, at: '2026-08-13T00:00:00Z'}
sources:
  - {id: valid, resource: /sources/valid.md.source}
---

# Valid
"""
    (bundle / "valid.md").write_text(valid, encoding="utf-8")
    (bundle / "legacy.md").write_text(
        "---\ntype: Feature\ntitle: Legacy\ntimestamp: 2026-01-01\nstatus: canonical\n---\n",
        encoding="utf-8",
    )
    private = bundle / ".GIT"
    private.mkdir()
    (private / "secret.md").write_text(valid.replace("Valid", "Secret"), encoding="utf-8")
    (bundle / "index.md").write_text(
        '---\nokf_version: "0.2"\n---\n', encoding="utf-8"
    )
    output = tmp_path / "viewer.html"

    stats = generate_visualization(bundle, output)
    html = output.read_text(encoding="utf-8")

    assert stats["concepts"] == 1
    assert '"id": "valid"' in html
    assert '"id": "legacy"' not in html
    assert '"id": ".GIT/secret"' not in html


def test_visualization_defuses_stored_xss_payloads(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    breakout = '</script><script>globalThis.VIZ_PWNED = true</script><b>&\u2028\u2029'
    frontmatter = {
        "type": breakout,
        "title": breakout,
        "description": '<img src=x onerror="globalThis.VIZ_PWNED=true">',
        "tags": ["<svg/onload=globalThis.VIZ_PWNED=true>"],
        "status": "stable",
        "generated": {"by": "process:test", "at": "2026-08-13T10:00:00Z"},
        "resource": "javascript:globalThis.VIZ_PWNED=true",
        "sources": [
            {
                "id": "evil",
                "resource": "data:text/html,<script>globalThis.VIZ_PWNED=true</script>",
                "title": breakout,
            }
        ],
    }
    body = """# Payload

<script>globalThis.VIZ_PWNED = true</script>
<img src="x" onerror="globalThis.VIZ_PWNED = true">
<iframe srcdoc="<script>globalThis.VIZ_PWNED = true</script>"></iframe>
[bad scheme](javascript:globalThis.VIZ_PWNED=true)
[data scheme](data:text/html,boom)
[safe external](https://example.com/path)
[safe relative](../safe.md)
"""
    (bundle / "evil.md").write_text(
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        + "---\n"
        + body,
        encoding="utf-8",
    )
    (bundle / "index.md").write_text(
        '---\nokf_version: "0.2"\n---\n', encoding="utf-8"
    )
    output = tmp_path / "viewer.html"

    generate_visualization(bundle, output, bundle_name=breakout)
    html = output.read_text(encoding="utf-8")

    # Script-element breakout characters and JS line separators are escaped in
    # both the bundle name and graph JSON. Decoding in JavaScript preserves the
    # original display text without letting the HTML parser terminate the script.
    assert breakout not in html
    assert "\u2028" not in html and "\u2029" not in html
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in html
    assert "\\u0026\\u2028\\u2029" in html

    # Markdown is parsed only into a local allowlist fragment; every URL-bearing
    # element is gated and internal links no longer manufacture javascript: hrefs.
    assert "SAFE_MARKDOWN_TAGS" in html
    assert "sanitizeMarkdown(renderedBody)" in html
    assert "const safeHref = safeUrl(href)" in html
    assert 'setAttribute("href", "javascript:void(0)")' not in html
    assert "__BUNDLE_" not in html and "__VIZ_" not in html
