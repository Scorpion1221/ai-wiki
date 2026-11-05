"""The writer reads the same unchanged files many times per changeset (2026-11-06: ~56 s a changeset on
the writer host), so parses, signatures and digests are cached by content. These tests pin what the
caches must never change: an edited file is always read afresh, a caller can never alter a cached
value, errors are never cached, and the bundle walks give exactly the old answers."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from aiwiki.engine import digests, document, validate
from aiwiki.runtime import policy

CONCEPT = """---
type: Reference
title: Cache probe
description: A concept for the cache tests.
tags: [test]
status: stable
generated: {by: 'process:test', at: '2026-08-13T10:00:00Z'}
sources:
  - {id: s-probe, resource: /sources/probe.md.source}
---

# Summary

Body {n}.
"""


def test_load_yaml_returns_a_private_copy_each_time() -> None:
    text = "a: &x {k: [1, 2]}\nb: *x\n"
    first = document.load_yaml(text)
    first["a"]["k"].append(3)
    second = document.load_yaml(text)
    assert second == {"a": {"k": [1, 2]}, "b": {"k": [1, 2]}}
    assert second["a"] is second["b"]  # an alias stays one object, as yaml.safe_load gives it


def test_load_yaml_never_caches_an_error() -> None:
    bad = "type: [unclosed\n"
    for _ in range(2):
        with pytest.raises(Exception, match="expected"):
            document.load_yaml(bad)


def test_a_large_text_is_parsed_but_not_held(monkeypatch) -> None:
    monkeypatch.setattr(document, "_YAML_CACHE_TEXT_MAX", 10)
    before = len(document._YAML_CACHE)
    assert document.load_yaml("key: a value longer than ten\n") == {"key": "a value longer than ten"}
    assert len(document._YAML_CACHE) == before


def _write(path: Path, n: int) -> None:
    path.write_text(CONCEPT.replace("{n}", str(n)), encoding="utf-8")


def test_judged_follows_every_edit_and_hands_out_copies(tmp_path: Path) -> None:
    path = tmp_path / "c.md"
    _write(path, 1)
    fm, body, substantive, generated = policy._judged(path)
    assert (substantive, generated) == (policy._substantive_signature(fm, body), policy._generated_signature(fm))
    fm["title"] = "changed by a caller"
    assert policy._judged(path)[0]["title"] == "Cache probe"  # the cached mapping is untouched
    _write(path, 2)
    assert policy._judged(path)[2] != substantive  # a new text, a new judgment
    _write(path, 1)
    assert policy._judged(path)[2] == substantive


def test_judged_gives_two_paths_with_the_same_text_their_own_results(tmp_path: Path) -> None:
    one, two = tmp_path / "one.md", tmp_path / "two.md"
    _write(one, 7)
    _write(two, 7)
    a, b = policy._judged(one), policy._judged(two)
    assert a[1:] == b[1:] and a[0] == b[0] and a[0] is not b[0]


def test_judged_refuses_a_symlink_like_parse_doc(tmp_path: Path) -> None:
    target = tmp_path / "t.md"
    _write(target, 1)
    link = tmp_path / "l.md"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlinks are not allowed"):
        policy._judged(link)


def test_file_digest_follows_a_rewrite_with_the_same_size_and_mtime(tmp_path: Path) -> None:
    path = tmp_path / "evidence.bin"
    path.write_bytes(b"aaaa")
    first = digests.file_sha256(path)
    stat = path.stat()
    time.sleep(0.01)
    path.write_bytes(b"bbbb")  # same size
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # and the old mtime: ctime still moves
    assert digests.file_sha256(path) != first
    path.write_bytes(b"aaaa")
    assert digests.file_sha256(path) == first


def test_validation_walk_matches_the_old_walks(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    (root / "metrics").mkdir(parents=True)
    (root / "index.md").write_text('---\nokf_version: "0.2"\n---\n', encoding="utf-8")
    _write(root / "metrics" / "a.md", 1)
    (root / ".okf" / "jobs").mkdir(parents=True)
    (root / ".okf" / "jobs" / "x.md").write_text("not a concept\n", encoding="utf-8")
    (root / "metrics" / "dir.md").mkdir()
    outside = tmp_path / "outside.md"
    _write(outside, 2)
    (root / "metrics" / "link.md").symlink_to(outside)
    (root / ".okf" / "l.md").symlink_to(outside)

    old_files = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames[:] = sorted(n for n in dirnames if n != ".git" and not (base / n).is_symlink())
        old_files += [base / n for n in sorted(filenames)
                      if (base / n).suffix == ".md" and not (base / n).is_symlink() and (base / n).is_file()]
    files, errors = validate._walk(root)
    assert files == sorted(old_files)
    assert errors == [".okf/l.md: symlinks are not allowed in an OKF bundle",
                      "metrics/link.md: symlinks are not allowed in an OKF bundle"]
    outside_okf = [p for p in root.rglob("*.md") if ".okf" not in p.relative_to(root).parts]
    assert document.markdown_paths(root) == sorted(outside_okf)
