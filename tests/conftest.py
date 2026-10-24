from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """A host's injected AIWIKI_TOKEN/AIWIKI_PRINCIPALS would override the tests' own auth."""
    monkeypatch.delenv("AIWIKI_TOKEN", raising=False)
    monkeypatch.delenv("AIWIKI_PRINCIPALS", raising=False)


@pytest.fixture(autouse=True)
def _no_host_runs(monkeypatch, tmp_path_factory):
    """`ai-wiki update` refuses while a run in the host's ~/.ai-wiki/state is live; a test never sees that one."""
    from aiwiki.cli import update

    state = tmp_path_factory.mktemp("state")
    monkeypatch.setattr(update, "_default_state_dir", lambda: state)
