"""Smoke test on a neutral fixture bundle: engine validates, service reads + searches."""
from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    _write(root / "sources" / "src-a.md", """
        ---
        source_type: manual
        ingested: 2026-01-01T00:00:00Z
        ---
        The HTTP cache stores responses keyed by URL with a TTL.
    """)
    _write(root / "topics" / "http-cache.md", """
        ---
        type: Reference
        title: HTTP cache
        description: How the response cache keys and expires entries.
        tags: [http, cache]
        timestamp: 2026-01-01T00:00:00Z
        status: reviewed
        confidence: high
        source_type: manual
        source_ref: sources/src-a.md
        sources: [sources/src-a.md]
        ---
        # Summary

        Responses are cached by URL with a TTL.

        # Citations

        - sources/src-a.md
    """)
    _write(root / "index.md", "# KB\n\nRoot index.\n")
    _write(root / "index-meta.yaml", "title: KB\ndescription: fixture\ndirectories:\n  topics: Reference topics.\n")
    return root


def _client(bundle: Path, monkeypatch):
    monkeypatch.setenv("AIWIKI_BUNDLE", str(bundle))
    monkeypatch.delenv("AIWIKI_BUNDLES", raising=False)
    monkeypatch.setenv("AIWIKI_TOKEN", "testtok")
    monkeypatch.setenv("AIWIKI_CURATE", "off")
    from aiwiki.service import app as appmod
    importlib.reload(appmod)
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


AUTH = {"Authorization": "Bearer testtok"}


def test_engine_validates(bundle: Path) -> None:
    from aiwiki.engine import validate
    assert validate.main([str(bundle)]) == 0


def test_service_reads_and_searches(bundle: Path, monkeypatch) -> None:
    c = _client(bundle, monkeypatch)
    assert c.get("/health").status_code == 401  # auth required
    assert c.get("/health", headers=AUTH).json()["concepts"] == 1
    root = {i["path"]: i for i in c.get("/ls", headers=AUTH).json()["items"]}
    assert root["topics/"]["kind"] == "dir"
    body = c.get("/cat", params={"path": "topics/http-cache.md"}, headers=AUTH).json()
    assert "# Citations" in body["content"]
    hits = c.get("/search", params={"q": "cache"}, headers=AUTH).json()["results"]
    assert hits and hits[0]["path"] == "topics/http-cache.md"
    # path traversal is rejected
    assert c.get("/cat", params={"path": "../../etc/passwd"}, headers=AUTH).status_code == 400
