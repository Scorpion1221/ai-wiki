"""Equivalence and cache-correctness tests for Stage 1 write-path optimizations.

Every optimization must leave gate verdicts, validation errors, lint high findings,
index outputs and source ledgers 100% identical to the uncached/full-copy paths.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiwiki.engine import gen_indexes, lint as lint_mod, scan_sources, validate
from aiwiki.engine.document import load_yaml
from aiwiki.runtime import audit, changeset, policy
from aiwiki.service import ingest as I


def _concept(
    title: str,
    *,
    body: str = "Summary body.",
    status: str = "stable",
    extra_fm: str = "",
    sources: str = "  - {id: s1, resource: /sources/s1.md.source}\n",
) -> str:
    return (
        "---\n"
        "type: Reference\n"
        f"title: {title}\n"
        f"description: Description for {title}.\n"
        "tags: [test]\n"
        f"status: {status}\n"
        "generated: {by: 'process:test', at: '2026-10-01T00:00:00Z'}\n"
        f"{extra_fm}"
        "sources:\n"
        f"{sources}"
        "---\n\n"
        "# Summary\n\n"
        f"{body}\n"
    )


def _make_bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SCHEMA.md").write_text("---\ntype: Contract\n---\n# Schema\n", encoding="utf-8")
    (root / "purpose.md").write_text("---\ntype: Contract\n---\n# Purpose\n", encoding="utf-8")
    (root / "log.md").write_text("# Log\n\n## 2026-10-01\n* **Creation**: init\n", encoding="utf-8")
    (root / "sources").mkdir(exist_ok=True)
    (root / "sources" / "s1.md.source").write_text("Frozen fact alpha=42.\n", encoding="utf-8")
    (root / "sources" / "extra.md.source").write_text("Unused large source.\n" * 100, encoding="utf-8")
    (root / "viz.html").write_text("<html>big viz</html>\n" * 100, encoding="utf-8")
    (root / "features").mkdir(exist_ok=True)
    (root / "features" / "a.md").write_text(
        _concept("Alpha", body="See [Beta](b.md) and fact alpha=42.[^s1]\n\n[^s1]: s1"),
        encoding="utf-8",
    )
    (root / "features" / "b.md").write_text(
        _concept("Beta", body="See [Alpha](a.md)[^s1]\n\n[^s1]: s1"),
        encoding="utf-8",
    )
    gen_indexes.generate_indexes(root)
    assert scan_sources.main([str(root), "--commit"]) == 0
    return root


def test_lint_high_for_paths_matches_full_lint_exactly(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path / "kb")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (root / "features" / "sym.md").symlink_to(outside)

    # Decoy index.md files inside .okf and .git must be ignored by lint.
    (root / ".okf" / "maint" / "items" / "it_1").mkdir(parents=True)
    (root / ".okf" / "maint" / "items" / "it_1" / "index.md").write_text("[Broken](missing.md)\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "index.md").write_text("[Broken](missing.md)\n", encoding="utf-8")

    # Add concepts covering every high-severity lint check + medium checks.
    (root / "features" / "broken_links.md").write_text(
        _concept(
            "Broken Links",
            body=(
                "Valid [Alpha](a.md)\n"
                "Unresolved [Missing](alpa.md)\n"
                "Root unresolved [MissingRoot](/features/nope.md)\n"
                "Escapes [Out](../../outside.md)\n"
                "Symlink [Sym](sym.md)\n"
                "External [Web](https://example.com/a.md)\n"
            ),
        ),
        encoding="utf-8",
    )
    (root / "features" / "contra.md").write_text(
        _concept(
            "Contradictions",
            extra_fm=(
                "contested: true\n"
                "contradictions:\n"
                "  - a.md\n"
                "  - nonexistent.md\n"
                "  - sym.md\n"
                "  - ../../outside.md\n"
            ),
        ),
        encoding="utf-8",
    )

    full_findings, _count = lint_mod.lint(root)
    assert not any(f["where"].startswith((".okf/", ".git/")) for f in full_findings)

    subsets = [
        [],
        ["features/a.md"],
        ["features/broken_links.md"],
        ["features/contra.md"],
        ["features/broken_links.md", "features/contra.md"],
        ["features/a.md", "features/b.md", "features/broken_links.md", "features/contra.md"],
        ["features/does-not-exist.md"],
    ]
    for subset in subsets:
        expected = [f for f in full_findings if f["severity"] == "high" and f["where"] in set(subset)]
        actual = lint_mod.lint_high_for_paths(root, subset)
        assert actual == expected, f"mismatch for subset={subset}"


def test_validate_cache_never_serves_stale_or_invalid_results(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path / "kb")
    target = root / "features" / "a.md"

    # 1. Valid bundle populates cache.
    assert validate.validate(root) == []
    assert validate.validate(root) == []

    # 2. Mutating a concept to invalid (profile error) must immediately report error.
    orig = target.read_text(encoding="utf-8")
    target.write_text(orig.replace("status: stable", "status: bogus"), encoding="utf-8")
    errs = validate.validate(root)
    assert any("invalid status" in e for e in errs)
    assert any("invalid status" in e for e in validate.validate_profile(root))

    # 3. Mutating to conformance error (missing type) must immediately report error.
    target.write_text(orig.replace("type: Reference\n", ""), encoding="utf-8")
    errs = validate.validate_okf_conformance(root)
    assert any("missing required frontmatter key type" in e for e in errs)

    # 4. Fixing restores clean pass.
    target.write_text(orig, encoding="utf-8")
    assert validate.validate(root) == []


def test_gen_indexes_meta_and_scan_sources_caches_and_dumper(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path / "kb")
    target = root / "features" / "a.md"

    m1 = gen_indexes._meta(target)
    assert m1["title"] == "Alpha"
    m1["title"] = "mutated by caller"
    assert gen_indexes._meta(target)["title"] == "Alpha"

    # Edit title and confirm cache tracks change.
    orig = target.read_text(encoding="utf-8")
    target.write_text(orig.replace("title: Alpha", "title: Alpha Two"), encoding="utf-8")
    assert gen_indexes._meta(target)["title"] == "Alpha Two"

    # scan_sources preflight catches newly introduced legacy string source immediately.
    scan_sources._preflight_concepts(root)
    target.write_text(orig.replace("  - {id: s1, resource: /sources/s1.md.source}", "  - /sources/s1.md.source"), encoding="utf-8")
    with pytest.raises(ValueError, match="legacy string sources"):
        scan_sources._preflight_concepts(root)
    target.write_text(orig, encoding="utf-8")
    scan_sources._preflight_concepts(root)

    # CSafeDumper writes valid YAML round-tripping long >80-char and unicode filenames.
    long_name = "中文-" + ("a" * 110) + ".md.source"
    (root / "sources" / long_name).write_text("unicode payload\n", encoding="utf-8")
    assert scan_sources.main([str(root), "--commit"]) == 0
    ledger = load_yaml((root / "sources" / ".hashes.yaml").read_text(encoding="utf-8"))
    assert f"sources/{long_name}" in ledger
    assert ledger == scan_sources._current_sources(root)


def test_isolated_concept_bundle_skips_sources_and_viz_but_keeps_symlinks_stray_md_and_collision(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path / "kb")
    (root / "sources" / "stray.md").write_text(_concept("Stray"), encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "sources" / "evil.source").symlink_to(outside)

    tmp, ws = policy._isolated_concept_bundle(root, keep_sources=frozenset({"s1.md.source"}))
    try:
        assert not (ws / "viz.html").exists()
        assert not (ws / "sources" / "extra.md.source").exists()
        assert not (ws / "sources" / ".hashes.yaml").exists()
        assert (ws / "sources" / "s1.md.source").is_file()
        assert (ws / "sources" / "stray.md").is_file()
        assert (ws / "sources" / "evil.source").is_symlink()
        assert (ws / "features" / "a.md").is_file()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # If viz.html itself is a symlink, it MUST NOT be ignored so symlink checks still catch it.
    (root / "viz.html").unlink()
    (root / "viz.html").symlink_to(outside)
    tmp2, ws2 = policy._isolated_concept_bundle(root)
    try:
        assert (ws2 / "viz.html").is_symlink()
    finally:
        import shutil
        shutil.rmtree(tmp2, ignore_errors=True)


def test_changeset_and_audit_evaluate_match_full_copy(tmp_path: Path, monkeypatch) -> None:
    root = _make_bundle(tmp_path / "kb")
    now = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
    ev_bytes = b"New observation beta=99.\n"
    import base64
    ev_b64 = base64.b64encode(ev_bytes).decode("ascii")

    # 1. Curate changeset referencing existing /sources/s1.md.source AND new evidence:packet
    a_before = (root / "features" / "a.md").read_text(encoding="utf-8")
    a_after = (
        a_before.replace(
            "  - {id: s1, resource: /sources/s1.md.source}\n",
            "  - {id: s1, resource: /sources/s1.md.source}\n  - {id: e2, resource: evidence:packet}\n",
        )
        + "\nNew beta=99.[^e2]\n\n[^e2]: e2\n"
    )
    req = {
        "schema": changeset.SCHEMA,
        "kind": "curate",
        "intent": "evidence",
        "base_revision": "0" * 40,
        "evidence": {"id": "e2", "upload": {"filename": "note.md", "content_b64": ev_b64}},
        "files": [{"op": "put", "path": "features/a.md", "base": changeset.content_hash(a_before), "content": a_after}],
    }

    lean_verdict = changeset.evaluate(root, req, actor="process:test", now=now)
    assert lean_verdict["status"] == "would_apply", lean_verdict

    # Compare against full _isolated_agent_bundle copy
    monkeypatch.setattr(policy, "_isolated_concept_bundle", lambda b, **_: policy._isolated_agent_bundle(b))
    full_verdict = changeset.evaluate(root, req, actor="process:test", now=now)
    assert lean_verdict == full_verdict

    # 1b. Packet path collision (existing source file with different bytes) must match full copy identically.
    monkeypatch.undo()
    collision_path = root / lean_verdict["source"]
    collision_path.write_bytes(b"corrupted different bytes\n")
    lean_col = changeset.evaluate(root, req, actor="process:test", now=now)
    monkeypatch.setattr(policy, "_isolated_concept_bundle", lambda b, **_: policy._isolated_agent_bundle(b))
    full_col = changeset.evaluate(root, req, actor="process:test", now=now)
    assert lean_col["status"] == "rejected"
    assert lean_col == full_col
    collision_path.unlink()

    # 2. Citing a nonexistent /sources/ file or /sources/inbox/ file must STILL fail resource_unresolvable
    monkeypatch.undo()
    (root / "sources" / "inbox").mkdir(exist_ok=True)
    (root / "sources" / "inbox" / "in.md.source").write_text("inbox only\n", encoding="utf-8")
    bad_after = a_after.replace("/sources/s1.md.source", "/sources/inbox/in.md.source")
    bad_req = {**req, "files": [{**req["files"][0], "content": bad_after}]}
    bad_verdict = changeset.evaluate(root, bad_req, actor="process:test", now=now)
    assert bad_verdict["status"] == "rejected"
    assert any(e["code"] == "resource_unresolvable" for e in bad_verdict["errors"])

    # 3. Audit evaluate_review (corrected verdict reads cited sources for _narrowing)
    corrected_a = a_before.replace("and fact alpha=42.", "and fact alpha=42 (reported).")
    audit_req = {
        "schema": changeset.SCHEMA,
        "kind": "audit",
        "base_revision": "0" * 40,
        "reviews": [{
            "path": "features/a.md",
            "base": changeset.content_hash(a_before),
            "verdict": "corrected",
            "content": corrected_a,
            "note": "Narrowed claim.",
        }],
    }
    lean_rev = audit.evaluate_review(
        root, audit_req, actor="process:ai-wiki-auditor", now=now,
        scope=None, auditors=frozenset({"process:ai-wiki-auditor"}),
    )
    assert lean_rev["status"] == "would_apply", lean_rev
    monkeypatch.setattr(policy, "_isolated_concept_bundle", lambda b, **_: policy._isolated_agent_bundle(b))
    full_rev = audit.evaluate_review(
        root, audit_req, actor="process:ai-wiki-auditor", now=now,
        scope=None, auditors=frozenset({"process:ai-wiki-auditor"}),
    )
    assert lean_rev == full_rev


def test_concept_names_cache_tracks_edits_and_deprecation(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path / "kb")
    names = changeset._concept_names(root)
    assert names.get("alpha") == "features/a.md"
    assert names.get("beta") == "features/b.md"

    a = root / "features" / "a.md"
    orig = a.read_text(encoding="utf-8")
    a.write_text(orig.replace("title: Alpha", "title: Gamma"), encoding="utf-8")
    names2 = changeset._concept_names(root)
    assert "alpha" not in names2 and names2.get("gamma") == "features/a.md"

    a.write_text(orig.replace("status: stable", "status: deprecated"), encoding="utf-8")
    names3 = changeset._concept_names(root)
    assert "alpha" not in names3
