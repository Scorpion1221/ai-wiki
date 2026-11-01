"""The C frontmatter loader: same values as the Python parser, same error messages."""
from __future__ import annotations

import yaml

from aiwiki.engine import document
from aiwiki.engine.document import OKFDocumentError, load_yaml, parse_document

FRONTMATTERS = [
    "type: Metric\ntitle: 'MRR: monthly'\ntags: [a, b]\n",
    "generated:\n  by: process:ai-wiki-maintainer\n  at: '2026-10-31T00:00:00Z'\nverified:\n"
    "- {by: process:ai-wiki-auditor, at: 2026-10-30T20:40:34Z}\n",
    "stale_after: 2026-12-31\nsources:\n- id: s1\n  resource: /sources/x.md.source\n  title: \"含中文：值\"\n",
    "description: >-\n  folded\n  text\nnumbers: [1, 2.5, 0x10, 1e3, .inf]\nflags: [yes, no, on, off, ~]\n",
    "",
]


def test_values_match_the_python_parser() -> None:
    assert document._FAST_LOADER is not None  # the wheels in uv.lock carry libyaml
    for text in FRONTMATTERS:
        assert load_yaml(text) == yaml.safe_load(text), text


def test_a_yaml_error_keeps_the_python_parsers_message() -> None:
    text = "---\ntitle: [unclosed\ntype: Metric\n---\nbody\n"
    try:
        yaml.safe_load("title: [unclosed\ntype: Metric")
    except yaml.YAMLError as exc:
        expected = f"invalid YAML frontmatter: {exc}"
    try:
        parse_document(text)
    except OKFDocumentError as exc:
        assert str(exc) == expected
    else:
        raise AssertionError("an unclosed flow sequence must be refused")


def test_without_libyaml_the_python_parser_is_used(monkeypatch) -> None:
    monkeypatch.setattr(document, "_FAST_LOADER", None)
    assert load_yaml(FRONTMATTERS[0]) == yaml.safe_load(FRONTMATTERS[0])
