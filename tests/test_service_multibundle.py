"""Multi-bundle service: one server hosts many bundles under AIWIKI_BUNDLES; clients pick
one with ?bundle=. Covers listing, selection, the ambiguous-default guard, create + delete."""
from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

AUTH = {"Authorization": "Bearer testtok"}


def _bundle(root: Path, name: str, concept_title: str) -> None:
    b = root / name
    (b / "sources").mkdir(parents=True, exist_ok=True)
    (b / "index.md").write_text(f"# {name}\n", encoding="utf-8")
    (b / "topics" / f"{name}.md").parent.mkdir(parents=True, exist_ok=True)
    (b / "topics" / f"{name}.md").write_text(textwrap.dedent(f"""
        ---
        type: Reference
        title: {concept_title}
        tags: [{name}]
        status: reviewed
        ---
        # Summary

        {concept_title} lives in bundle {name}.
    """).lstrip("\n"), encoding="utf-8")


def _client(root: Path, monkeypatch, disable: str = "", default: str | None = None):
    monkeypatch.setenv("AIWIKI_BUNDLES", str(root))
    monkeypatch.delenv("AIWIKI_BUNDLE", raising=False)
    monkeypatch.setenv("AIWIKI_TOKEN", "testtok")
    monkeypatch.setenv("AIWIKI_CURATE", "off")
    monkeypatch.setenv("AIWIKI_DISABLE", disable)
    if default is None:
        monkeypatch.delenv("AIWIKI_DEFAULT_BUNDLE", raising=False)
    else:
        monkeypatch.setenv("AIWIKI_DEFAULT_BUNDLE", default)
    from aiwiki.service import app as appmod
    importlib.reload(appmod)
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "bundles"
    _bundle(r, "kb-a", "Alpha topic")
    _bundle(r, "kb-b", "Beta topic")
    return r


def test_lists_bundles_with_counts(root: Path, monkeypatch) -> None:
    c = _client(root, monkeypatch)
    assert c.get("/bundles").status_code == 401  # auth required
    d = c.get("/bundles", headers=AUTH).json()
    by = {b["name"]: b["concepts"] for b in d["bundles"]}
    assert by == {"kb-a": 1, "kb-b": 1}
    assert d["default"] is None  # two bundles, no AIWIKI_DEFAULT_BUNDLE → no implicit default


def test_bundle_selection_and_isolation(root: Path, monkeypatch) -> None:
    c = _client(root, monkeypatch)
    # omitting ?bundle= with >1 bundle and no default is a 400, not a silent guess
    assert c.get("/health", headers=AUTH).status_code == 400
    a = c.get("/search", params={"q": "Alpha", "bundle": "kb-a"}, headers=AUTH).json()["results"]
    assert a and a[0]["path"] == "topics/kb-a.md"
    # Alpha does not leak into kb-b
    assert c.get("/search", params={"q": "Alpha", "bundle": "kb-b"}, headers=AUTH).json()["results"] == []
    assert c.get("/health", params={"bundle": "kb-b"}, headers=AUTH).json()["bundle"] == "kb-b"
    assert c.get("/cat", params={"path": "x.md", "bundle": "nope"}, headers=AUTH).status_code == 404


def test_default_bundle_env(root: Path, monkeypatch) -> None:
    c = _client(root, monkeypatch, default="kb-b")
    assert c.get("/bundles", headers=AUTH).json()["default"] == "kb-b"
    assert c.get("/health", headers=AUTH).json()["bundle"] == "kb-b"  # used when ?bundle= omitted


def test_create_and_delete_bundle(root: Path, monkeypatch) -> None:
    c = _client(root, monkeypatch)
    r = c.post("/bundles", json={"name": "kb-new"}, headers=AUTH)
    assert r.status_code == 201 and r.json()["name"] == "kb-new"
    assert "kb-new" in {b["name"] for b in c.get("/bundles", headers=AUTH).json()["bundles"]}
    assert (root / "kb-new" / "purpose.md").is_file()  # scaffolded a valid bundle
    assert c.post("/bundles", json={"name": "kb-new"}, headers=AUTH).status_code == 409  # dup
    assert c.post("/bundles", json={"name": "../escape"}, headers=AUTH).status_code == 400  # name gate
    assert c.delete("/bundles/kb-new", headers=AUTH).status_code == 200
    assert not (root / "kb-new").exists()
    assert c.delete("/bundles/kb-new", headers=AUTH).status_code == 404


def test_create_delete_gated_by_disable(root: Path, monkeypatch) -> None:
    c = _client(root, monkeypatch, disable="create,delete")
    assert c.post("/bundles", json={"name": "kb-x"}, headers=AUTH).status_code == 403
    assert c.delete("/bundles/kb-a", headers=AUTH).status_code == 403
