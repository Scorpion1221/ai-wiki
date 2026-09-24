from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """A host's injected AIWIKI_TOKEN/AIWIKI_PRINCIPALS would override the tests' own auth."""
    monkeypatch.delenv("AIWIKI_TOKEN", raising=False)
    monkeypatch.delenv("AIWIKI_PRINCIPALS", raising=False)
