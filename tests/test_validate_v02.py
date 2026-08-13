"""OKF v0.2 conformance and the stricter AI Wiki producer profile."""
from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

import pytest
import yaml

from aiwiki.engine import validate
from aiwiki.engine.document import (
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    TRUST_HUMAN_REVIEWED,
    TRUST_MACHINE_CONFIRMED,
    TRUST_UNVERIFIED,
    freshness,
    normalize_verified,
    trust_tier,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    _write(root / "index.md", """
        ---
        okf_version: "0.2"
        ---
        # Fixture
    """)
    _write(root / "log.md", """
        # Change log

        ## 2026-08-13
        * **Creation**: Added the fixture.
    """)
    _write(root / "purpose.md", """
        ---
        type: Contract
        ---
        # Purpose
    """)
    _write(root / "concepts" / "cache.md", """
        ---
        type: Reference
        title: HTTP cache
        description: How the cache expires responses.
        tags: [http, cache]
        status: stable
        generated:
          by: process:test-curator
          at: 2026-08-13T01:02:03Z
        verified:
          by: human:reviewer
          at: 2026-08-13T02:03:04Z
        stale_after: 2026-09-01
        sources:
          - id: source-a
            resource: references/source-a.txt
            last_modified: 2026-08-12
        extension_key: retained
        ---
        # Summary

        Cached responses expire.[^source-a]

        [^source-a]: Source A
    """)
    return root


def _attested(
    root: Path,
    *,
    body: str = "# Computation\n\n```sql\nSELECT 1\n```\n",
    **overrides: object,
) -> Path:
    frontmatter: dict[str, object] = {
        "type": "Attested Computation",
        "title": "Sanctioned query",
        "description": "The approved deterministic query.",
        "tags": ["computation"],
        "status": "stable",
        "generated": {"by": "process:test-curator", "at": "2026-08-13T01:02:03Z"},
        "sources": [{"id": "policy", "resource": "references/policy.txt"}],
        "runtime": "bigquery",
        "executor": {"resource": "references/run-query.md"},
        "attester": {"resource": "references/attest.py"},
    }
    frontmatter.update(overrides)
    path = root / "computations" / "query.md"
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    _write(path, f"---\n{dumped}---\n\n{body}")
    return path


def test_valid_v02_bundle_passes_both_layers(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    assert validate.validate_okf_conformance(root) == []
    assert validate.validate_profile(root) == []
    assert validate.validate(root) == []
    assert validate.main([str(root)]) == 0
    assert validate.main([str(root), "--conformance-only"]) == 0


def test_exact_conformance_is_permissive_but_checks_all_markdown(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    _write(root / "minimal.md", """
        ---
        type: Vendor Extension
        unknown: accepted
        ---
        Body with [a broken link](missing.md).
    """)
    assert validate.validate_okf_conformance(root) == []
    assert any(
        "minimal.md: missing required frontmatter key title" in error
        for error in validate.validate_profile(root)
    )

    _write(root / "sources" / "raw.md", "raw source without frontmatter\n")
    assert "sources/raw.md: missing YAML frontmatter" in validate.validate_okf_conformance(root)


def test_conformance_and_profile_reject_symlinks_without_reading_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _bundle(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\ntype: Reference\ntitle: External secret\n---\nSENTINEL\n",
        encoding="utf-8",
    )
    link = root / "concepts" / "escape.md"
    link.symlink_to(outside)

    original_load = validate.load_document

    def guarded_load(path: Path, *, require_frontmatter: bool = True):
        assert not path.is_symlink(), "validator attempted to read through a symlink"
        return original_load(path, require_frontmatter=require_frontmatter)

    monkeypatch.setattr(validate, "load_document", guarded_load)
    expected = "concepts/escape.md: symlinks are not allowed in an OKF bundle"
    assert expected in validate.validate_okf_conformance(root)
    assert expected in validate.validate_profile(root)
    assert outside.read_text(encoding="utf-8").endswith("SENTINEL\n")


def test_validator_rejects_non_markdown_source_and_directory_symlinks(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    outside_file = tmp_path / "outside.bin"
    outside_file.write_bytes(b"sentinel")
    sources = root / "sources"
    sources.mkdir()
    (sources / "escape.bin").symlink_to(outside_file)

    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "valid.md").write_text("---\ntype: Reference\n---\n", encoding="utf-8")
    (root / "linked-dir").symlink_to(outside_dir, target_is_directory=True)

    errors = validate.validate(root)
    assert "sources/escape.bin: symlinks are not allowed in an OKF bundle" in errors
    assert "linked-dir: symlinks are not allowed in an OKF bundle" in errors
    assert not any("linked-dir/valid.md" in error for error in errors)


@pytest.mark.parametrize("status", ["reviewed", "canonical", "stale", "ready"])
def test_profile_rejects_legacy_and_unknown_statuses(tmp_path: Path, status: str) -> None:
    root = _bundle(tmp_path)
    concept = root / "concepts" / "cache.md"
    concept.write_text(
        concept.read_text(encoding="utf-8").replace("status: stable", f"status: {status}"),
        encoding="utf-8",
    )
    assert any(f"invalid status {status!r}" in error for error in validate.validate_profile(root))


@pytest.mark.parametrize("legacy_key", ["timestamp", "last_verified_at"])
def test_profile_rejects_legacy_frontmatter_keys(tmp_path: Path, legacy_key: str) -> None:
    root = _bundle(tmp_path)
    concept = root / "concepts" / "cache.md"
    text = concept.read_text(encoding="utf-8").replace("status: stable", f"status: stable\n{legacy_key}: 2026-08-13")
    concept.write_text(text, encoding="utf-8")
    assert any(f"legacy frontmatter key {legacy_key}" in error for error in validate.validate_profile(root))


def test_profile_rejects_string_sources_and_legacy_citations(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    concept = root / "concepts" / "cache.md"
    text = concept.read_text(encoding="utf-8")
    start = text.index("sources:\n")
    end = text.index("extension_key:", start)
    text = text[:start] + "sources: [references/source-a.txt]\n" + text[end:]
    text += "\n# Citations\n\n- references/source-a.txt\n"
    concept.write_text(text, encoding="utf-8")
    errors = validate.validate_profile(root)
    assert any("legacy string sources" in error for error in errors)
    assert any("legacy # Citations" in error for error in errors)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("generated: process:test", "generated must be a mapping"),
        ("generated: {by: test, at: 2026-08-13T01:02:03Z}", "generated.by must use"),
        ("generated: {by: process:test, at: 2026-08-13}", "generated.at must be"),
    ],
)
def test_profile_validates_generated(tmp_path: Path, replacement: str, message: str) -> None:
    root = _bundle(tmp_path)
    concept = root / "concepts" / "cache.md"
    text = concept.read_text(encoding="utf-8")
    begin = text.index("generated:\n")
    end = text.index("verified:\n", begin)
    concept.write_text(text[:begin] + replacement + "\n" + text[end:], encoding="utf-8")
    assert any(message in error for error in validate.validate_profile(root))


def test_profile_accepts_bare_or_list_verified_and_rejects_bad_events(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    assert validate.validate_profile(root) == []  # fixture uses the bare mapping form

    concept = root / "concepts" / "cache.md"
    text = concept.read_text(encoding="utf-8")
    begin = text.index("verified:\n")
    end = text.index("stale_after:", begin)
    list_verified = """verified:
  - {by: process:nightly, at: 2026-08-13T02:03:04Z}
  - {by: human:reviewer, at: 2026-08-13T03:04:05Z}
"""
    concept.write_text(text[:begin] + list_verified + text[end:], encoding="utf-8")
    assert validate.validate_profile(root) == []

    concept.write_text((text[:begin] + "verified: [{by: process:nightly}]\n" + text[end:]), encoding="utf-8")
    assert any("verified[0].at" in error for error in validate.validate_profile(root))


@pytest.mark.parametrize("bad", ["2026-9-01", "2026-02-30", "2026-09-01T00:00:00Z"])
def test_profile_rejects_bad_stale_after(tmp_path: Path, bad: str) -> None:
    root = _bundle(tmp_path)
    concept = root / "concepts" / "cache.md"
    concept.write_text(
        concept.read_text(encoding="utf-8").replace("stale_after: 2026-09-01", f"stale_after: {bad}"),
        encoding="utf-8",
    )
    errors = validate.validate(root)
    assert any(
        "stale_after must be YYYY-MM-DD" in error or "invalid YAML frontmatter" in error
        for error in errors
    )


def test_reserved_index_and_log_structure(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    _write(root / "concepts" / "index.md", """
        ---
        okf_version: "0.2"
        ---
        # Not allowed here
    """)
    _write(root / "log.md", """
        # Log
        ## [2026-08-13]
        * legacy heading
        ## 2026-02-30
        * impossible date
    """)
    errors = validate.validate_okf_conformance(root)
    assert any("only the bundle-root index.md" in error for error in errors)
    assert any("got '[2026-08-13]'" in error for error in errors)
    assert any("valid calendar date" in error for error in errors)


def test_official_style_log_frontmatter_is_conformant(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    _write(root / "log.md", """
        ---
        type: Log
        title: Change log
        ---
        # Change log
        ## 2026-08-13
        * Added a concept.
    """)

    assert not [
        error for error in validate.validate_okf_conformance(root)
        if error.startswith("log.md:")
    ]


def test_log_dates_are_unique_and_newest_first(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    _write(root / "log.md", """
        # Change log
        ## 2026-08-13
        * latest
        ## 2026-08-12
        * older
    """)
    assert not [error for error in validate.validate_okf_conformance(root) if error.startswith("log.md:")]

    _write(root / "log.md", """
        # Change log
        ## 2026-08-12
        * older first
        ## 2026-08-13
        * latest last
    """)
    errors = validate.validate_okf_conformance(root)
    assert "log.md: log date headings must be unique and newest first" in errors

    _write(root / "log.md", """
        # Change log
        ## 2026-08-13
        * one
        ## 2026-08-13
        * duplicate
    """)
    errors = validate.validate_okf_conformance(root)
    assert "log.md: log date heading must not be repeated; got '2026-08-13'" in errors


def test_attested_computation_accepts_inline_or_file_contract(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    _attested(root)
    assert validate.validate_profile(root) == []

    _attested(
        root,
        computation="references/computations/query.sql",
        body="# Notes\n\nThe computation is stored as an artifact.\n",
    )
    assert validate.validate_profile(root) == []


def test_attested_computation_contract_is_strict_profile_only(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    _write(root / "minimal-computation.md", """
        ---
        type: Attested Computation
        ---
        No runtime contract yet.
    """)
    assert validate.validate_okf_conformance(root) == []
    errors = validate.validate_profile(root)
    assert any("Attested Computation.runtime" in error for error in errors)
    assert any("Attested Computation.executor" in error for error in errors)
    assert any("Attested Computation.attester" in error for error in errors)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"runtime": ""}, "Attested Computation.runtime must be a non-empty string"),
        ({"executor": {}}, "Attested Computation.executor.resource must be a non-empty string"),
        ({"executor": "run"}, "Attested Computation.executor must be a mapping"),
        ({"attester": {}}, "Attested Computation.attester.resource must be a non-empty string"),
        ({"attester": "check"}, "Attested Computation.attester must be a mapping"),
    ],
)
def test_attested_computation_requires_runtime_executor_and_attester(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    root = _bundle(tmp_path)
    _attested(root, **overrides)
    assert any(message in error for error in validate.validate_profile(root))


@pytest.mark.parametrize(
    ("overrides", "body", "message"),
    [
        (
            {"computation": "references/query.sql"},
            "# Computation\n\n```sql\nSELECT 1\n```\n",
            "must use computation path or one inline # Computation fence, not both",
        ),
        (
            {"computation": "references/query.sql"},
            "# Computation\n\nNo inline code.\n",
            "must use computation path or one inline # Computation fence, not both",
        ),
        ({}, "# Notes\n\nNo computation.\n", "must have exactly one # Computation section"),
        (
            {},
            "# Computation\n\n```sql\nSELECT 1\n```\n\n```sql\nSELECT 2\n```\n",
            "must contain exactly one fenced code block",
        ),
        ({"computation": ""}, "# Notes\n", "computation must be a non-empty path string"),
    ],
)
def test_attested_computation_requires_exactly_one_computation_form(
    tmp_path: Path,
    overrides: dict[str, object],
    body: str,
    message: str,
) -> None:
    root = _bundle(tmp_path)
    _attested(root, body=body, **overrides)
    assert any(message in error for error in validate.validate_profile(root))


def test_empty_scaffold_is_valid_v02_profile(tmp_path: Path) -> None:
    from aiwiki.service.bundle import scaffold

    root = tmp_path / "empty"
    scaffold(root, "empty")
    assert validate.validate(root) == []


def test_profile_requires_root_version_and_every_strict_field(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    _write(root / "index.md", "# No version\n")
    _write(root / "concepts" / "cache.md", """
        ---
        type: Reference
        ---
        Body
    """)
    errors = validate.validate_profile(root)
    assert 'index.md: okf_version must be "0.2"' in errors
    for key in ("title", "description", "tags", "status", "generated", "sources"):
        assert f"concepts/cache.md: missing required frontmatter key {key}" in errors


def test_read_signal_helpers() -> None:
    assert normalize_verified({"verified": {"by": "process:test", "at": "2026-08-13T00:00:00Z"}}) == [
        {"by": "process:test", "at": "2026-08-13T00:00:00Z"}
    ]
    assert trust_tier({}) == TRUST_UNVERIFIED
    assert trust_tier({"verified": {"by": "process:test", "at": "2026-08-13T00:00:00Z"}}) == TRUST_MACHINE_CONFIRMED
    assert trust_tier({"verified": [{"by": "human:q", "at": "2026-08-13T00:00:00Z"}]}) == TRUST_HUMAN_REVIEWED
    assert freshness({"stale_after": "2026-08-14"}, date(2026, 8, 13)) == FRESHNESS_FRESH
    assert freshness({"stale_after": "2026-08-13"}, date(2026, 8, 13)) == FRESHNESS_STALE
