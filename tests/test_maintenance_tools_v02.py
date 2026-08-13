from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from aiwiki.engine import append_log, backfill_sources, scan_sources, update_concept


def _write_concept(path: Path, frontmatter: dict, body: str = "# Summary\n\nDurable fact.\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    path.write_text(f"---\n{dumped}---\n{body}", encoding="utf-8")


def _read_concept(path: Path) -> tuple[dict, str]:
    return update_concept._parse(path.read_text(encoding="utf-8"))


def _base_frontmatter(**overrides) -> dict:
    value = {
        "type": "Feature",
        "title": "Example",
        "description": "Example concept",
        "tags": ["old-tag"],
        "status": "stable",
        "generated": {"by": "process:seed", "at": "2026-08-12T00:00:00Z"},
    }
    value.update(overrides)
    return value


def test_update_concept_preserves_v02_provenance_and_verification(tmp_path):
    bundle = tmp_path / "bundle"
    concept = bundle / "features" / "example.md"
    prior_body = "# Summary\n\n" + ("Durable fact with detail. " * 20) + "\n"
    _write_concept(
        concept,
        _base_frontmatter(
            sources=[
                {
                    "id": "source-a",
                    "resource": "/sources/a.md",
                    "title": "Original source title",
                    "author": "team:product",
                }
            ],
            contradictions=["risks/old.md"],
            verified={"by": "human:owner", "at": "2026-08-12T01:00:00Z"},
        ),
        prior_body,
    )

    assert update_concept.main(["snapshot", str(bundle), "features/example.md"]) == 0

    _write_concept(
        concept,
        _base_frontmatter(
            title="Accidentally renamed",
            tags=["new-tag"],
            sources=[
                {"id": "source-a", "resource": "/sources/a.md"},
                {"id": "source-b", "resource": "/sources/b.md", "title": "New source"},
            ],
            contradictions=["risks/new.md"],
            verified=[{"by": "process:daily-audit", "at": "2026-08-13T01:00:00Z"}],
        ),
        prior_body + "\nAdditional detail.\n",
    )

    assert update_concept.main(
        [
            "enforce",
            str(bundle),
            "features/example.md",
            "--generated-by",
            "process:test-maintainer",
        ]
    ) == 0

    fm, body = _read_concept(concept)
    assert fm["title"] == "Example"
    assert fm["tags"] == ["new-tag", "old-tag"]
    assert fm["contradictions"] == ["risks/new.md", "risks/old.md"]
    assert [source["resource"] for source in fm["sources"]] == [
        "/sources/a.md",
        "/sources/b.md",
    ]
    assert fm["sources"][0]["title"] == "Original source title"
    assert fm["sources"][0]["author"] == "team:product"
    assert fm["verified"] == [
        {"by": "process:daily-audit", "at": "2026-08-13T01:00:00Z"},
        {"by": "human:owner", "at": "2026-08-12T01:00:00Z"},
    ]
    assert fm["generated"]["by"] == "process:test-maintainer"
    assert fm["generated"]["at"].endswith("Z")
    assert "last_verified_at" not in fm
    assert "timestamp" not in fm
    assert "# Citations" not in body


def test_update_concept_rejects_legacy_sources(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    concept = bundle / "features" / "legacy.md"
    _write_concept(concept, _base_frontmatter(sources=["sources/raw.md"]))

    assert update_concept.main(["snapshot", str(bundle), "features/legacy.md"]) == 2
    assert "legacy string sources are not supported" in capsys.readouterr().err
    assert not (bundle / ".okf" / "history").exists()


def test_update_concept_rejects_symlink_without_reading_or_writing_target(
    tmp_path, capsys
):
    bundle = tmp_path / "bundle"
    (bundle / "features").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("external secret", encoding="utf-8")
    (bundle / "features" / "escape.md").symlink_to(outside)

    assert update_concept.main(["snapshot", str(bundle), "features/escape.md"]) == 2
    assert outside.read_text(encoding="utf-8") == "external secret"
    assert not (bundle / ".okf" / "history").exists()
    assert "unsafe concept path" in capsys.readouterr().err


def test_scan_sources_uses_exact_structured_resource_only(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    source = bundle / "sources" / "raw.md.source"
    source.parent.mkdir(parents=True)
    source.write_text("first version\n", encoding="utf-8")
    _write_concept(
        bundle / "features" / "linked.md",
        _base_frontmatter(sources=[{"id": "raw", "resource": "/sources/raw.md.source"}]),
    )
    _write_concept(
        bundle / "features" / "body-only.md",
        _base_frontmatter(),
        "# Summary\n\nThe raw source filename appears here: raw.md.\n",
    )

    assert scan_sources.main([str(bundle), "--commit"]) == 0
    capsys.readouterr()
    source.write_text("second version\n", encoding="utf-8")

    assert scan_sources.main([str(bundle), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["changed"] == ["sources/raw.md.source"]
    assert report["affected_concepts"] == {
        "sources/raw.md.source": ["features/linked.md"],
    }


def test_scan_sources_hashes_text_snapshot_frontmatter_as_raw_bytes(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    sources = bundle / "sources"
    sources.mkdir(parents=True)
    initial = b"---\nrevision: one\n---\nunchanged body\n"
    changed = b"---\nrevision: two\n---\nunchanged body\n"
    paths = [sources / "evidence.txt", sources / "evidence.md"]
    for path in paths:
        path.write_bytes(initial)

    assert scan_sources.main([str(bundle), "--commit"]) == 0
    capsys.readouterr()
    baseline = yaml.safe_load((sources / ".hashes.yaml").read_text(encoding="utf-8"))
    assert baseline == {
        "sources/evidence.md": hashlib.sha256(initial).hexdigest(),
        "sources/evidence.txt": hashlib.sha256(initial).hexdigest(),
    }

    for path in paths:
        path.write_bytes(changed)

    assert scan_sources.main([str(bundle), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["changed"] == ["sources/evidence.md", "sources/evidence.txt"]


def test_scan_sources_rejects_legacy_string_sources_even_without_drift(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    source = bundle / "sources" / "raw.md.source"
    source.parent.mkdir(parents=True)
    source.write_text("stable\n", encoding="utf-8")
    _write_concept(
        bundle / "features" / "legacy.md",
        _base_frontmatter(sources=["sources/raw.md.source"]),
    )

    # Establish the baseline manually so the run has no drift; schema preflight
    # still must reject the legacy concept.
    hashes = bundle / "sources" / ".hashes.yaml"
    hashes.write_text(
        yaml.safe_dump({"sources/raw.md.source": scan_sources._hash_file(source)}),
        encoding="utf-8",
    )
    assert scan_sources.main([str(bundle)]) == 2
    assert "legacy string sources are not supported" in capsys.readouterr().err


def test_scan_sources_rejects_other_v01_contract_fields(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    source = bundle / "sources" / "raw.md.source"
    source.parent.mkdir(parents=True)
    source.write_text("stable\n", encoding="utf-8")
    _write_concept(
        bundle / "features" / "legacy.md",
        _base_frontmatter(timestamp="2026-08-12T00:00:00Z"),
    )

    assert scan_sources.main([str(bundle)]) == 2
    assert "legacy fields are not supported: timestamp" in capsys.readouterr().err


def test_scan_sources_rejects_source_symlink_without_hashing_target(tmp_path, capsys, monkeypatch):
    bundle = tmp_path / "bundle"
    sources = bundle / "sources"
    sources.mkdir(parents=True)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("sentinel outside bundle\n", encoding="utf-8")
    link = sources / "escape.txt"
    link.symlink_to(outside)

    original_hash = scan_sources._hash_file

    def guarded_hash(path: Path) -> str:
        assert not path.is_symlink(), "source scanner attempted to hash a symlink target"
        return original_hash(path)

    monkeypatch.setattr(scan_sources, "_hash_file", guarded_hash)
    assert scan_sources.main([str(bundle), "--commit"]) == 2
    assert "sources/escape.txt: source snapshots must not be symlinks" in capsys.readouterr().err
    assert outside.read_text(encoding="utf-8") == "sentinel outside bundle\n"
    assert not (sources / ".hashes.yaml").exists()


def test_scan_sources_rejects_symlinked_source_directory(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside-sources"
    outside.mkdir()
    (outside / "secret.txt").write_text("sentinel\n", encoding="utf-8")
    (bundle / "sources").symlink_to(outside, target_is_directory=True)

    assert scan_sources.main([str(bundle)]) == 2
    assert "source directory must not be a symlink" in capsys.readouterr().err


def test_scan_sources_rejects_concept_symlink_without_reading_target(
    tmp_path, capsys, monkeypatch
):
    bundle = tmp_path / "bundle"
    concepts = bundle / "features"
    concepts.mkdir(parents=True)
    outside = tmp_path / "outside-concept.md"
    _write_concept(outside, _base_frontmatter(title="External sentinel"))
    link = concepts / "escape.md"
    link.symlink_to(outside)

    original_read = Path.read_text

    def guarded_read(path: Path, *args, **kwargs):
        assert path != link, "source scanner attempted to read a symlinked concept"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    before = original_read(outside, encoding="utf-8")
    assert scan_sources.main([str(bundle), "--commit"]) == 2
    assert "features/escape.md: concept paths must not contain symlinks" in capsys.readouterr().err
    assert original_read(outside, encoding="utf-8") == before


def test_scan_sources_rejects_symlinked_hash_ledger_before_read(tmp_path, capsys, monkeypatch):
    bundle = tmp_path / "bundle"
    sources = bundle / "sources"
    sources.mkdir(parents=True)
    outside = tmp_path / "outside-hashes.yaml"
    outside.write_text("sources/secret: external-sentinel\n", encoding="utf-8")
    ledger = sources / ".hashes.yaml"
    ledger.symlink_to(outside)

    original_read = Path.read_text

    def guarded_read(path: Path, *args, **kwargs):
        assert path != ledger, "source scanner attempted to read a symlinked hash ledger"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    assert scan_sources.main([str(bundle)]) == 2
    assert "sources/.hashes.yaml: source snapshots must not be symlinks" in capsys.readouterr().err


def test_backfill_sources_writes_structured_objects(tmp_path):
    bundle = tmp_path / "bundle"
    source = bundle / "sources" / "raw-evidence.md.source"
    source.parent.mkdir(parents=True)
    source.write_text("raw evidence\n", encoding="utf-8")
    concept = bundle / "features" / "example.md"
    _write_concept(concept, _base_frontmatter())

    assert backfill_sources.main([str(bundle), "--write"]) == 0
    fm, _body = _read_concept(concept)
    assert fm["sources"] == [
        {
            "id": "raw-evidence",
            "resource": "/sources/raw-evidence.md.source",
            "title": "raw-evidence",
        }
    ]
    assert backfill_sources.main([str(bundle), "--write"]) == 0


def test_backfill_sources_preflight_prevents_partial_write_on_legacy_input(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    source = bundle / "sources" / "raw.md.source"
    source.parent.mkdir(parents=True)
    source.write_text("raw\n", encoding="utf-8")
    clean = bundle / "features" / "clean.md"
    _write_concept(clean, _base_frontmatter())
    _write_concept(
        bundle / "features" / "legacy.md",
        _base_frontmatter(sources=["sources/raw.md.source"]),
    )
    before = clean.read_text(encoding="utf-8")

    assert backfill_sources.main([str(bundle), "--write"]) == 2
    assert clean.read_text(encoding="utf-8") == before
    assert "legacy string sources are not supported" in capsys.readouterr().err


def test_backfill_sources_rejects_legacy_status_without_partial_write(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    source = bundle / "sources" / "raw.md.source"
    source.parent.mkdir(parents=True)
    source.write_text("raw\n", encoding="utf-8")
    concept = bundle / "features" / "legacy.md"
    _write_concept(concept, _base_frontmatter(status="reviewed"))
    before = concept.read_text(encoding="utf-8")

    assert backfill_sources.main([str(bundle), "--write"]) == 2
    assert concept.read_text(encoding="utf-8") == before
    assert "legacy status 'reviewed' is not supported" in capsys.readouterr().err


def test_backfill_sources_rejects_source_symlink(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    sources = bundle / "sources"
    sources.mkdir(parents=True)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("sentinel outside bundle\n", encoding="utf-8")
    (sources / "escape.txt").symlink_to(outside)

    assert backfill_sources.main([str(bundle), "--write"]) == 2
    assert "sources/escape.txt: source snapshots must not be symlinks" in capsys.readouterr().err
    assert outside.read_text(encoding="utf-8") == "sentinel outside bundle\n"


def test_backfill_sources_rejects_symlinked_concept_ancestor_without_external_write(
    tmp_path, capsys, monkeypatch
):
    bundle = tmp_path / "bundle"
    source = bundle / "sources" / "raw.md.source"
    source.parent.mkdir(parents=True)
    source.write_text("raw evidence\n", encoding="utf-8")

    outside_dir = tmp_path / "outside-concepts"
    external = outside_dir / "escape.md"
    _write_concept(external, _base_frontmatter(title="External sentinel"))
    (bundle / "features").symlink_to(outside_dir, target_is_directory=True)
    before = external.read_text(encoding="utf-8")

    original_read = Path.read_text
    original_write = Path.write_text

    def is_external(path: Path) -> bool:
        try:
            path.resolve().relative_to(outside_dir.resolve())
        except ValueError:
            return False
        return True

    def guarded_read(path: Path, *args, **kwargs):
        assert not is_external(path), "backfill attempted to read through a symlinked ancestor"
        return original_read(path, *args, **kwargs)

    def guarded_write(path: Path, *args, **kwargs):
        assert not is_external(path), "backfill attempted to write through a symlinked ancestor"
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    monkeypatch.setattr(Path, "write_text", guarded_write)
    assert backfill_sources.main([str(bundle), "--write"]) == 2
    assert "features: concept paths must not contain symlinks" in capsys.readouterr().err
    assert original_read(external, encoding="utf-8") == before


def test_append_log_groups_iso_dates_newest_first(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    assert append_log.main([str(bundle), "ingest", "Older item", "--date", "2026-08-12"]) == 0
    assert append_log.main([str(bundle), "ingest", "First today", "--date", "2026-08-13"]) == 0
    assert append_log.main(
        [
            str(bundle),
            "audit",
            "Second today",
            "--note",
            "machine verified",
            "--files",
            "features/a.md",
            "--date",
            "2026-08-13",
        ]
    ) == 0

    text = (bundle / "log.md").read_text(encoding="utf-8")
    assert "## [" not in text
    assert text.count("## 2026-08-13") == 1
    assert text.index("## 2026-08-13") < text.index("## 2026-08-12")
    assert text.index("**Audit**: Second today") < text.index("**Ingest**: First today")
    assert "machine verified — files: features/a.md" in text


def test_append_log_rejects_legacy_heading_without_modifying_file(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    log = bundle / "log.md"
    legacy = "# Update Log\n\n## [2026-08-12] ingest | old\n"
    log.write_text(legacy, encoding="utf-8")

    assert append_log.main([str(bundle), "audit", "new", "--date", "2026-08-13"]) == 2
    assert log.read_text(encoding="utf-8") == legacy
    assert "headings must be '## YYYY-MM-DD'" in capsys.readouterr().err


def test_append_log_rejects_symlink_without_writing_external_target(tmp_path, capsys):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "external-log.md"
    outside.write_text("external ledger\n", encoding="utf-8")
    (bundle / "log.md").symlink_to(outside)

    assert append_log.main([str(bundle), "audit", "unsafe", "--date", "2026-08-13"]) == 2
    assert outside.read_text(encoding="utf-8") == "external ledger\n"
    assert "unsafe log path" in capsys.readouterr().err
