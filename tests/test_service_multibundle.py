"""Multi-bundle service: one server hosts many bundles under AIWIKI_BUNDLES; clients pick
one with ?bundle=. Covers listing, selection, the ambiguous-default guard, create + delete."""
from __future__ import annotations

import importlib
import json
import textwrap
import threading
import time
from pathlib import Path

import pytest

AUTH = {"Authorization": "Bearer testtok"}


def _bundle(root: Path, name: str, concept_title: str) -> None:
    b = root / name
    (b / "sources").mkdir(parents=True, exist_ok=True)
    (b / "index.md").write_text(
        f'---\nokf_version: "0.2"\n---\n\n# {name}\n', encoding="utf-8",
    )
    (b / "topics" / f"{name}.md").parent.mkdir(parents=True, exist_ok=True)
    (b / "topics" / f"{name}.md").write_text(textwrap.dedent(f"""
        ---
        type: Reference
        title: {concept_title}
        description: Test concept for bundle isolation.
        tags: [{name}]
        status: draft
        generated: {{by: process:test, at: 2026-08-13T00:00:00Z}}
        sources:
          - {{id: fixture, resource: https://example.com/{name}}}
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


def test_ls_rejects_parent_absolute_and_symlink_escape(root: Path, monkeypatch) -> None:
    outside = root.parent / "private"
    outside.mkdir()
    (outside / "payroll.md").write_text(
        "---\ntype: Secret\ntitle: Payroll\ndescription: salary secret\n---\n",
        encoding="utf-8",
    )
    (root / "kb-a" / "escape").symlink_to(outside, target_is_directory=True)
    client = _client(root, monkeypatch)

    for escaped in ("..", str(outside), "escape"):
        response = client.get(
            "/ls", params={"bundle": "kb-a", "dir": escaped}, headers=AUTH,
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "path escapes bundle"

    root_listing = client.get("/ls", params={"bundle": "kb-a"}, headers=AUTH).json()["items"]
    assert all(item.get("title") != "Payroll" and item.get("path") != "escape/" for item in root_listing)


def test_git_metadata_is_never_exposed_even_with_show_all(root: Path, monkeypatch) -> None:
    sentinel = "P0_GIT_CONFIG_SENTINEL"
    git_dir = root / "kb-a" / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(sentinel, encoding="utf-8")
    client = _client(root, monkeypatch)

    responses = [
        client.get("/ls", params={"bundle": "kb-a", "show_all": True}, headers=AUTH),
        client.get(
            "/ls",
            params={"bundle": "kb-a", "recursive": True, "show_all": True},
            headers=AUTH,
        ),
        client.get("/cat", params={"bundle": "kb-a", "path": ".git/config"}, headers=AUTH),
        client.get("/ls", params={"bundle": "kb-a", "dir": ".git"}, headers=AUTH),
        client.get("/search", params={"bundle": "kb-a", "q": sentinel}, headers=AUTH),
        client.get("/grep", params={"bundle": "kb-a", "q": sentinel}, headers=AUTH),
    ]
    assert responses[2].status_code == 400
    assert responses[3].status_code == 400
    assert client.get(
        "/cat", params={"bundle": "kb-a", "path": ".GIT/config"}, headers=AUTH,
    ).status_code == 400
    assert all(sentinel not in response.text for response in responses)
    assert all(".git" not in response.text for response in responses[:2])


def test_invalid_profile_concept_has_no_api_metadata_or_read_surface(root: Path, monkeypatch) -> None:
    concept = root / "kb-a" / "topics" / "kb-a.md"
    concept.write_text(
        concept.read_text(encoding="utf-8").replace("status: draft\n", ""),
        encoding="utf-8",
    )
    client = _client(root, monkeypatch)

    assert client.get(
        "/search", params={"bundle": "kb-a", "q": "Alpha"}, headers=AUTH,
    ).json()["results"] == []
    assert client.get("/health", params={"bundle": "kb-a"}, headers=AUTH).json()["concepts"] == 0
    listing = client.get(
        "/ls", params={"bundle": "kb-a", "dir": "topics"}, headers=AUTH,
    ).json()["items"]
    assert listing == [{
        "kind": "invalid",
        "path": "topics/kb-a.md",
        "name": "kb-a.md",
        "description": "invalid strict OKF v0.2 concept",
    }]
    document = client.get(
        "/cat", params={"bundle": "kb-a", "path": "topics/kb-a.md"}, headers=AUTH,
    ).json()
    assert "Alpha topic" in document["content"] and "metadata" not in document
    assert client.get(
        "/links", params={"bundle": "kb-a", "path": "topics/kb-a.md"}, headers=AUTH,
    ).status_code == 404


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
    ignored = (root / "kb-new" / ".gitignore").read_text()
    assert ".okf/" in ignored  # job records never committed
    assert "sources/inbox/" in ignored  # uncurated sources never committed
    assert c.post("/bundles", json={"name": "kb-new"}, headers=AUTH).status_code == 409  # dup
    assert c.post("/bundles", json={"name": "../escape"}, headers=AUTH).status_code == 400  # name gate
    assert c.delete("/bundles/kb-new", headers=AUTH).status_code == 200
    assert not (root / "kb-new").exists()
    assert c.delete("/bundles/kb-new", headers=AUTH).status_code == 404


def test_delete_rejects_active_job_then_allows_same_name_recreate(root: Path, monkeypatch) -> None:
    c = _client(root, monkeypatch)
    assert c.post("/bundles", json={"name": "kb-new"}, headers=AUTH).status_code == 201
    bundle = root / "kb-new"
    job_path = bundle / ".okf" / "jobs" / "queued.json"
    job_path.write_text(json.dumps({
        "id": "queued", "kind": "ingest", "status": "queued",
        "source": "sources/inbox/source.md.source",
    }), encoding="utf-8")

    blocked = c.delete("/bundles/kb-new", headers=AUTH)
    assert blocked.status_code == 409 and "queued/running jobs" in blocked.json()["detail"]
    assert bundle.is_dir()

    job = json.loads(job_path.read_text(encoding="utf-8"))
    job["status"] = "done"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    assert c.delete("/bundles/kb-new", headers=AUTH).status_code == 200
    assert not bundle.exists()
    assert c.post(
        "/ingest", params={"bundle": "kb-new"}, json={"text": "late source"}, headers=AUTH,
    ).status_code == 404
    assert not bundle.exists()  # no .okf zombie recreated by a stale receive path
    assert c.post("/bundles", json={"name": "kb-new"}, headers=AUTH).status_code == 201


def test_delete_fails_fast_while_worker_mutation_lock_is_held(root: Path, monkeypatch) -> None:
    c = _client(root, monkeypatch)
    from aiwiki.service import app as appmod

    with appmod.worker.serialized_mutation():
        ingest = c.post(
            "/ingest", params={"bundle": "kb-a"}, json={"text": "queue during worker"}, headers=AUTH,
        )
        response = c.delete("/bundles/kb-a", headers=AUTH)

    assert ingest.status_code == 200 and ingest.json()["status"] == "queued"
    assert response.status_code == 409
    assert (root / "kb-a").is_dir()


def test_writer_read_surfaces_fail_closed_during_mutation(root: Path, monkeypatch) -> None:
    c = _client(root, monkeypatch)
    from aiwiki.service import app as appmod

    requests = [
        ("/bundles", {}),
        ("/health", {"bundle": "kb-a"}),
        ("/ls", {"bundle": "kb-a"}),
        ("/cat", {"bundle": "kb-a", "path": "topics/kb-a.md"}),
        ("/grep", {"bundle": "kb-a", "q": "Alpha"}),
        ("/search", {"bundle": "kb-a", "q": "Alpha"}),
        ("/links", {"bundle": "kb-a", "path": "topics/kb-a.md"}),
        ("/log", {"bundle": "kb-a"}),
    ]
    with appmod.worker.serialized_mutation():
        for path, params in requests:
            response = c.get(path, params=params, headers=AUTH)
            assert response.status_code == 503
            assert "mutation in progress" in response.json()["detail"]


def test_read_window_closes_check_to_use_race_with_mutation(root: Path, monkeypatch) -> None:
    c = _client(root, monkeypatch)
    from aiwiki.service import app as appmod

    read_entered = threading.Event()
    release_read = threading.Event()
    mutation_entered = threading.Event()
    release_mutation = threading.Event()
    original_health = appmod.B.health

    def blocked_health(bundle: Path):
        read_entered.set()
        assert release_read.wait(2)
        return original_health(bundle)

    monkeypatch.setattr(appmod.B, "health", blocked_health)
    first: dict[str, object] = {}
    reader = threading.Thread(target=lambda: first.setdefault(
        "response",
        c.get("/health", params={"bundle": "kb-a"}, headers=AUTH),
    ))
    reader.start()
    assert read_entered.wait(2)

    def mutate() -> None:
        with appmod.worker.serialized_mutation():
            mutation_entered.set()
            assert release_mutation.wait(2)

    writer = threading.Thread(target=mutate)
    writer.start()
    deadline = time.monotonic() + 2
    while not appmod.worker.is_mutating() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert appmod.worker.is_mutating()  # pending behind the in-flight read
    assert not mutation_entered.is_set()

    # New reads fail rather than slipping between a one-shot flag check and B.*.
    assert c.get(
        "/health", params={"bundle": "kb-a"}, headers=AUTH,
    ).status_code == 503
    release_read.set()
    reader.join(2)
    assert first["response"].status_code == 200
    assert mutation_entered.wait(2)
    assert c.get(
        "/health", params={"bundle": "kb-a"}, headers=AUTH,
    ).status_code == 503
    release_mutation.set()
    writer.join(2)
    assert not reader.is_alive() and not writer.is_alive()


def test_create_delete_gated_by_disable(root: Path, monkeypatch) -> None:
    c = _client(root, monkeypatch, disable="create,delete")
    assert c.post("/bundles", json={"name": "kb-x"}, headers=AUTH).status_code == 403
    assert c.delete("/bundles/kb-a", headers=AUTH).status_code == 403
