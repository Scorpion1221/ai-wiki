from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from aiwiki.engine.document import concept_metadata, freshness, trust_tier
from aiwiki.runtime import curate
from aiwiki.service import bundle as B
from aiwiki.service import ingest as I


def _concept(
    root: Path,
    name: str,
    *,
    status: str = "stable",
    verified: str = "",
    stale_after: str = "2099-01-01",
    mentions: int = 1,
) -> None:
    body = " ".join(["needle"] * mentions)
    if not (root / "index.md").is_file():
        (root / "index.md").write_text(
            '---\nokf_version: "0.2"\n---\n\n# Test bundle\n', encoding="utf-8",
        )
    (root / "topics").mkdir(parents=True, exist_ok=True)
    (root / "topics" / f"{name}.md").write_text(
        f"""---
type: Reference
title: {name}
description: Test concept
tags: [test]
status: {status}
generated: {{by: process:test, at: 2026-08-13T10:00:00Z}}
{verified}stale_after: {stale_after}
sources:
  - {{id: source-{name}, resource: /sources/{name}.md.source}}
---

# Summary

{body}
""",
        encoding="utf-8",
    )


def test_okf_v02_metadata_derivation() -> None:
    assert trust_tier({}) == "unverified"
    assert trust_tier({"verified": {"by": "process:audit"}}) == "machine-confirmed"
    assert trust_tier({"verified": [{"by": "human:owner"}]}) == "human-reviewed"
    assert freshness({}) == "unspecified"
    assert freshness({"stale_after": "2026-08-13"}, date(2026, 8, 12)) == "fresh"
    assert freshness({"stale_after": "2026-08-13"}, date(2026, 8, 13)) == "stale"


def test_verification_before_current_generation_is_historical_only() -> None:
    frontmatter = {
        "generated": {"by": "process:curator", "at": "2026-08-13T12:00:00Z"},
        "verified": {"by": "human:owner", "at": "2026-08-13T11:00:00Z"},
    }
    metadata = concept_metadata(frontmatter)
    assert metadata["trust"] == "human-reviewed"  # SPEC §5.3 uses all verified history
    assert metadata["verification_current"] is False
    assert metadata["verified_by"] == ["human:owner"]
    assert metadata["verified_at"] == "2026-08-13T11:00:00Z"
    assert metadata["current_verified_by"] == []
    assert metadata["current_verified_at"] == ""
    assert metadata["last_verification_at"] == "2026-08-13T11:00:00Z"

    frontmatter["verified"] = [
        {"by": "human:owner", "at": "2026-08-13T11:00:00Z"},
        {"by": "process:audit", "at": "2026-08-13T12:00:00Z"},
    ]
    assert trust_tier(frontmatter) == "human-reviewed"
    metadata = concept_metadata(frontmatter)
    assert metadata["verification_current"] is True
    assert metadata["current_verified_by"] == ["process:audit"]
    assert metadata["current_verified_at"] == "2026-08-13T12:00:00Z"


def test_entries_and_search_expose_metadata_and_rank_relevance_first(tmp_path: Path) -> None:
    _concept(tmp_path, "human", verified="verified: {by: human:owner, at: 2026-08-13T12:00:00Z}\n")
    _concept(tmp_path, "machine", verified="verified: {by: process:audit, at: 2026-08-13T11:00:00Z}\n")
    _concept(tmp_path, "unverified")
    _concept(tmp_path, "draft", status="draft", mentions=10)
    _concept(tmp_path, "stale", stale_after="2020-01-01", mentions=20)
    _concept(tmp_path, "deprecated", status="deprecated", mentions=30)

    results = B.search(tmp_path, "needle", top_k=None)
    assert [item["title"] for item in results] == [
        "deprecated", "stale", "draft", "human", "machine", "unverified",
    ]
    human = next(item for item in results if item["title"] == "human")
    assert human | {
        "status": "stable",
        "trust": "human-reviewed",
        "freshness": "fresh",
        "generated_at": "2026-08-13T10:00:00+00:00",
        "verified_at": "2026-08-13T12:00:00+00:00",
    } == human
    entries = B.list_dir(tmp_path, "topics")
    assert all({
        "status", "trust", "freshness", "verification_current",
        "generated_at", "verified_at", "current_verified_at",
    } <= item.keys() for item in entries)


def test_absolute_bundle_links_and_log_prefixed_concepts_are_read(tmp_path: Path) -> None:
    _concept(tmp_path, "one")
    _concept(tmp_path, "two")
    _concept(tmp_path, "log-history")
    one = tmp_path / "topics" / "one.md"
    one.write_text(
        one.read_text(encoding="utf-8").replace("needle", "[Two](/topics/two.md)"),
        encoding="utf-8",
    )

    graph = B.links(tmp_path, "topics/one.md")
    assert [item["path"] for item in graph["outbound"]] == ["topics/two.md"]
    assert any(item["path"] == "topics/log-history.md" for item in B.search(tmp_path, "needle"))


def test_invalid_frontmatter_never_fails_open_as_stable(tmp_path: Path) -> None:
    _concept(tmp_path, "valid")
    broken = tmp_path / "topics" / "broken.md"
    broken.write_text("---\ntype: [unterminated\n---\nneedle\n", encoding="utf-8")

    assert [item["path"] for item in B.search(tmp_path, "needle")] == ["topics/valid.md"]
    assert B.health(tmp_path)["concepts"] == 1
    by_path = {item["path"]: item for item in B.list_dir(tmp_path, "topics")}
    assert by_path["topics/broken.md"]["kind"] == "invalid"


def test_profile_invalid_and_legacy_concepts_are_never_read_as_facts(tmp_path: Path) -> None:
    _concept(tmp_path, "valid")
    current = tmp_path / "topics" / "missing-status.md"
    current.write_text(
        """---
type: Feature
title: Missing status
description: released current fact
tags: [release]
generated: {by: process:test, at: 2026-08-13T00:00:00Z}
verified: {by: human:owner, at: 2026-08-13T01:00:00Z}
stale_after: 2099-01-01
sources: [{id: release, resource: https://example.com/release}]
---
# Summary

released current fact
""",
        encoding="utf-8",
    )
    legacy = tmp_path / "topics" / "legacy.md"
    legacy.write_text(
        """---
type: Feature
title: Legacy
description: legacy released fact
tags: [release]
status: canonical
timestamp: 2026-08-13
sources: [sources/legacy.md]
---
# Summary

legacy released fact
""",
        encoding="utf-8",
    )

    assert B.search(tmp_path, "released") == []
    assert B.metadata_for_path(tmp_path, current) is None
    entries = {entry["path"]: entry for entry in B.list_dir(tmp_path, "topics")}
    assert entries["topics/missing-status.md"]["kind"] == "invalid"
    assert entries["topics/legacy.md"]["kind"] == "invalid"
    assert B.health(tmp_path)["concepts"] == 1


def test_non_v02_bundle_exposes_no_concepts_or_trust_metadata(tmp_path: Path) -> None:
    _concept(tmp_path, "valid")
    concept = tmp_path / "topics" / "valid.md"
    (tmp_path / "index.md").write_text(
        '---\nokf_version: "0.1"\n---\n\n# Legacy bundle\n', encoding="utf-8",
    )

    assert list(B.concepts(tmp_path)) == []
    assert B.search(tmp_path, "needle") == []
    assert B.grep(tmp_path, "needle") == []
    assert B.health(tmp_path)["concepts"] == 0
    assert B.metadata_for_path(tmp_path, concept) is None
    assert B.list_dir(tmp_path, "topics")[0]["kind"] == "invalid"
    with pytest.raises(FileNotFoundError):
        B.links(tmp_path, "topics/valid.md")


def test_list_dir_rejects_parent_absolute_and_symlink_escape(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    outside = tmp_path / "outside"
    bundle.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    (bundle / "escape").symlink_to(outside, target_is_directory=True)

    for escaped in ("..", str(outside), "escape"):
        with pytest.raises(ValueError):
            B.list_dir(bundle, escaped)
    assert B.list_dir(bundle) == []  # the escaping symlink is not followed or exposed


def test_git_component_is_private_on_every_bundle_read_path(tmp_path: Path) -> None:
    _concept(tmp_path, "valid")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("P0_GIT_SENTINEL", encoding="utf-8")

    with pytest.raises(ValueError):
        B.safe_resolve(tmp_path, ".git/config")
    with pytest.raises(ValueError):
        B.list_dir(tmp_path, ".git", show_all=True)
    assert all(".git" not in item["path"] for item in B.list_dir(
        tmp_path, recursive=True, show_all=True,
    ))
    assert B.search(tmp_path, "P0_GIT_SENTINEL") == []
    assert B.grep(tmp_path, "P0_GIT_SENTINEL") == []


def test_all_read_surfaces_ignore_symlinked_concept_content(tmp_path: Path) -> None:
    sentinel = "P0_EXTERNAL_SENTINEL"
    bundle = tmp_path / "bundle"
    outside = tmp_path / "outside"
    bundle.mkdir()
    _concept(bundle, "valid")
    outside.mkdir()
    external = outside / "external.md"
    external.write_text(
        f"""---
type: Feature
title: {sentinel}
description: {sentinel}
tags: [secret]
status: stable
generated: {{by: process:test, at: 2026-08-13T00:00:00Z}}
verified: {{by: human:owner, at: 2026-08-13T01:00:00Z}}
stale_after: 2099-01-01
sources: [{{id: secret, resource: https://example.com/secret}}]
---
# Summary

{sentinel}
""",
        encoding="utf-8",
    )
    leaked = bundle / "topics" / "leaked.md"
    leaked.symlink_to(external)
    internal_alias = bundle / "topics" / "alias.md"
    internal_alias.symlink_to(bundle / "topics" / "valid.md")

    assert [rel for _path, rel in B.concepts(bundle)] == ["topics/valid.md"]
    assert B.search(bundle, sentinel) == []
    assert B.grep(bundle, sentinel) == []
    assert B.health(bundle)["concepts"] == 1
    assert {item["path"] for item in B.list_dir(bundle, "topics")} == {"topics/valid.md"}
    assert B.metadata_for_path(bundle, leaked) is None
    assert B.metadata_for_path(bundle, internal_alias) is None
    for link in ("topics/leaked.md", "topics/alias.md"):
        with pytest.raises(ValueError):
            B.safe_resolve(bundle, link)
        with pytest.raises(ValueError):
            B.links(bundle, link)


def test_symlinked_root_version_marker_cannot_enable_bundle_reads(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    outside = tmp_path / "outside"
    bundle.mkdir()
    outside.mkdir()
    external_index = outside / "index.md"
    external_index.write_text('---\nokf_version: "0.2"\n---\n', encoding="utf-8")
    (bundle / "index.md").symlink_to(external_index)
    (bundle / "topics").mkdir()
    concept = bundle / "topics" / "x.md"
    concept.write_text(
        """---
type: Feature
title: X
description: needle
tags: [x]
status: draft
generated: {by: process:test, at: 2026-08-13T00:00:00Z}
sources: [{id: x, resource: https://example.com/x}]
---
# Summary
needle
""",
        encoding="utf-8",
    )

    assert B.health(bundle)["okf_version"] == ""
    assert B.health(bundle)["concepts"] == 0
    assert B.search(bundle, "needle") == []


def test_health_reports_version_counts_and_git_revision(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text(
        '---\nokf_version: "0.2"\n---\n\n# Test bundle\n', encoding="utf-8")
    _concept(tmp_path, "machine", verified="verified: {by: process:audit, at: 2026-08-13T11:00:00Z}\n")
    _concept(tmp_path, "stale", stale_after="2020-01-01")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)

    data = B.health(tmp_path)
    assert data["okf_version"] == "0.2"
    assert data["concepts"] == 2
    assert data["by_trust"] == {"machine-confirmed": 1, "unverified": 1}
    assert data["by_freshness"] == {"fresh": 1, "stale": 1}
    assert data["git_revision"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True,
    ).strip()


def test_scaffold_is_okf_v02(tmp_path: Path) -> None:
    target = tmp_path / "new-kb"
    B.scaffold(target, "new-kb")

    assert B.parse((target / "index.md").read_text())[0] == {"okf_version": "0.2"}
    assert B.parse((target / "purpose.md").read_text())[0] == {"type": "Contract"}
    assert B.parse((target / "SCHEMA.md").read_text())[0] == {"type": "Contract"}


def test_commit_scaffold_initializes_independent_clean_repository(tmp_path: Path) -> None:
    target = tmp_path / "new-kb"
    B.scaffold(target, "new-kb")

    result = B.commit_scaffold(target, "new-kb")

    assert result["initialized"] is True and result["commit"]
    assert subprocess.check_output(
        ["git", "-C", str(target), "status", "--porcelain"], text=True,
    ).strip() == ""
    assert subprocess.check_output(
        ["git", "-C", str(target), "ls-files"], text=True,
    ).splitlines() == [".gitignore", "SCHEMA.md", "index-meta.yaml", "index.md", "log.md", "purpose.md"]


def test_commit_scaffold_isolates_bundle_from_enclosing_repository(tmp_path: Path) -> None:
    root = tmp_path / "bundles"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    (root / "README").write_text("bundles\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    parent_head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
    ).strip()
    target = root / "new-kb"
    B.scaffold(target, "new-kb")

    result = B.commit_scaffold(target, "new-kb")

    assert result["initialized"] is True and result["repository"] == str(target)
    assert (target / ".git").is_dir()
    assert subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain"], text=True,
    ).strip() == ""
    assert subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
    ).strip() == parent_head
    assert "index.md" in subprocess.check_output(
        ["git", "-C", str(target), "show", "--pretty=format:", "--name-only", "HEAD"], text=True,
    ).splitlines()


def test_created_bundle_first_ingest_has_clean_git_preflight(tmp_path: Path) -> None:
    target = tmp_path / "new-kb"
    B.scaffold(target, "new-kb")
    B.commit_scaffold(target, "new-kb")

    job, duplicate = I.receive_source(target, b"first evidence\n", filename="first.md")
    root = curate._repo_root(target)
    assert root == target and duplicate is False and job["status"] == "queued"
    curate._exclude_inbox(root, target)

    # The first job JSON and inbox source are operational, not knowledge changes;
    # curate.run's real Git-mode clean-tree gate therefore accepts this bundle.
    assert curate._working_files(root) == []
