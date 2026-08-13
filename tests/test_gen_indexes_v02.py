from pathlib import Path

import pytest

from aiwiki.engine.gen_indexes import generate_indexes


def test_root_index_declares_okf_v02_but_directory_indexes_do_not(tmp_path: Path):
    (tmp_path / "index-meta.yaml").write_text(
        "title: Test bundle\ndescription: Test description.\n",
        encoding="utf-8",
    )
    concepts = tmp_path / "features"
    concepts.mkdir()
    (concepts / "example.md").write_text(
        """---
type: Feature
title: Example
description: Example feature.
tags: [example]
status: draft
generated: {by: process:test, at: '2026-08-13T00:00:00Z'}
sources:
  - {id: example-source, resource: /sources/example.md.source}
---

# Summary

Example.
""",
        encoding="utf-8",
    )

    written, missing = generate_indexes(tmp_path)

    assert not missing
    assert tmp_path / "index.md" in written
    assert (tmp_path / "index.md").read_text(encoding="utf-8").startswith(
        '---\nokf_version: "0.2"\n---\n\n# Test bundle\n'
    )
    assert not (concepts / "index.md").read_text(encoding="utf-8").startswith("---\n")


def test_root_index_lists_all_top_level_source_snapshots(tmp_path: Path):
    (tmp_path / "index-meta.yaml").write_text("title: Test bundle\n", encoding="utf-8")
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "evidence.md.source").write_text("raw", encoding="utf-8")
    (sources / "report.pdf").write_bytes(b"pdf")
    (sources / ".hashes.yaml").write_text("{}", encoding="utf-8")

    generate_indexes(tmp_path)

    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "[evidence](sources/evidence.md.source)" in index
    assert "[report](sources/report.pdf)" in index
    assert "[evidence.md]" not in index
    assert ".hashes.yaml" not in index


def test_log_prefixed_concept_is_not_treated_as_reserved(tmp_path: Path):
    (tmp_path / "log-history.md").write_text(
        """---
type: Reference
title: Historical logging policy
description: Durable policy, not the reserved change log.
tags: [logging]
status: stable
generated: {by: process:test, at: '2026-08-13T00:00:00Z'}
sources:
  - {id: policy, resource: /sources/policy.md.source}
---
""",
        encoding="utf-8",
    )

    generate_indexes(tmp_path)

    assert "[Historical logging policy](log-history.md)" in (
        tmp_path / "index.md"
    ).read_text(encoding="utf-8")


def test_empty_bundle_without_sidecar_still_gets_v02_root_index(tmp_path: Path):
    written, missing = generate_indexes(tmp_path)

    assert written == [tmp_path / "index.md"]
    assert not missing
    assert (tmp_path / "index.md").read_text(encoding="utf-8") == (
        '---\nokf_version: "0.2"\n---\n'
    )


def test_generator_rejects_symlink_without_reading_external_concept(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text(
        "---\ntype: Feature\ntitle: External sentinel\ndescription: secret\n---\n",
        encoding="utf-8",
    )
    concepts = tmp_path / "features"
    concepts.mkdir()
    (concepts / "escape.md").symlink_to(outside)

    with pytest.raises(ValueError, match=r"features/escape\.md: symlinks are not allowed"):
        generate_indexes(tmp_path)
    assert outside.read_text(encoding="utf-8").startswith("---\ntype: Feature")
    assert not (tmp_path / "index.md").exists()


def test_log_prefixed_markdown_is_a_concept_not_a_reserved_file(tmp_path: Path):
    concept = tmp_path / "history" / "log-2026.md"
    concept.parent.mkdir()
    concept.write_text(
        """---
type: Reference
title: Historical log analysis
description: Analysis whose filename happens to start with log-.
tags: [history]
status: draft
generated: {by: process:test, at: '2026-08-13T00:00:00Z'}
sources:
  - {id: raw-log, resource: /sources/raw-log.txt}
---

# Summary

This is a normal concept, not the reserved exact filename log.md.
""",
        encoding="utf-8",
    )

    written, missing = generate_indexes(tmp_path)

    assert not missing
    assert concept.parent / "index.md" in written
    directory_index = (concept.parent / "index.md").read_text(encoding="utf-8")
    assert "[Historical log analysis](log-2026.md)" in directory_index
