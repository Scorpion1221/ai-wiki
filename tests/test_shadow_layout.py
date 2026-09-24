"""The phase 2 writer layout (docs/phase2-shadow-runbook.md): one bundles root that holds the
shadow's own clone and a link to production's bundle, which stays at its own path.

A linked bundle must behave exactly like single-bundle mode on its real directory: the
registry hands out that directory, so the reads, the Git-root checks of curate, audit, revert
and recovery, the job files and the changeset gate all see the path they see today, while a
symlink inside the bundle is still refused.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from gate_fixture import METRIC, OPERATOR, Gate, clone, concept, git, wait_for
from test_service_multibundle import AUTH, _bundle, _client

from aiwiki.runtime import curate, revert
from aiwiki.runtime.audit import run as audit_run  # the Gate replaces audit.run with a recorder
from aiwiki.service import bundle as B
from aiwiki.service import ingest as I
from aiwiki.service import worker


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """``bundles/shadow`` is a real bundle; ``bundles/prod`` links to ``home/prod``."""
    root, home = tmp_path / "bundles", tmp_path / "home"
    _bundle(root, "shadow", "Shadow topic")
    _bundle(home, "prod", "Production topic")
    (root / "prod").symlink_to(home / "prod", target_is_directory=True)
    return root, (home / "prod").resolve()


def test_a_linked_bundle_is_served_from_its_real_directory(layout, monkeypatch) -> None:
    root, prod = layout
    assert B.discover(root) == {"prod": prod, "shadow": root / "shadow"}
    client = _client(root, monkeypatch, default="prod")

    listed = client.get("/bundles", headers=AUTH).json()
    assert listed == {"bundles": [{"name": "prod", "concepts": 1}, {"name": "shadow", "concepts": 1}],
                      "default": "prod"}
    assert client.get("/health", headers=AUTH).json()["bundle"] == "prod"
    found = client.get("/search", params={"q": "Production", "bundle": "prod"}, headers=AUTH).json()["results"]
    assert [row["path"] for row in found] == ["topics/prod.md"]
    assert client.get("/search", params={"q": "Production", "bundle": "shadow"}, headers=AUTH).json()["results"] == []
    document = client.get("/cat", params={"bundle": "prod", "path": "topics/prod.md"}, headers=AUTH).json()
    assert "Production topic" in document["content"] and document["metadata"]["status"] == "draft"


def test_a_symlink_inside_a_linked_bundle_is_still_refused(layout, monkeypatch) -> None:
    root, prod = layout
    outside = root.parent / "private"
    outside.mkdir()
    (outside / "payroll.md").write_text("---\ntype: Secret\ntitle: Payroll\n---\n", encoding="utf-8")
    (prod / "escape").symlink_to(outside, target_is_directory=True)
    (prod / "topics" / "alias.md").symlink_to(prod / "topics" / "prod.md")
    client = _client(root, monkeypatch)

    for rel in ("escape/payroll.md", "topics/alias.md"):
        assert client.get("/cat", params={"bundle": "prod", "path": rel}, headers=AUTH).status_code == 400
    assert client.get("/ls", params={"bundle": "prod", "dir": "escape"}, headers=AUTH).status_code == 400
    assert client.get("/health", params={"bundle": "prod"}, headers=AUTH).json()["concepts"] == 1


def test_only_a_same_name_link_to_a_real_bundle_is_discovered(tmp_path: Path) -> None:
    root, home = tmp_path / "bundles", tmp_path / "home"
    root.mkdir()
    _bundle(home, "prod", "Production topic")
    (home / "notes").mkdir()
    (home / "marked").mkdir()
    (home / "marked" / "index.md").symlink_to(home / "prod" / "index.md")  # a borrowed marker is no bundle
    links = {
        "alias": home / "prod",  # another name would dodge the worker checks keyed on bundle.name
        "notes": home / "notes",  # not a bundle
        "marked": home / "marked",
        "gone": home / "gone",  # dangling
        "loop": root / "loop",
        ".prod": home / "prod",  # hidden
    }
    for name, target in links.items():
        (root / name).symlink_to(target, target_is_directory=True)
    assert B.discover(root) == {}


def test_a_linked_bundle_cannot_be_deleted_or_recreated_through_the_server(layout, monkeypatch) -> None:
    root, prod = layout
    client = _client(root, monkeypatch)

    refused = client.delete("/bundles/prod", headers=AUTH)
    assert refused.status_code == 409 and "remove the link on the host" in refused.json()["detail"]
    assert (prod / "topics" / "prod.md").is_file() and (root / "prod").is_symlink()
    assert client.post("/bundles", json={"name": "prod"}, headers=AUTH).status_code == 409
    assert client.delete("/bundles/shadow", headers=AUTH).status_code == 200  # a real bundle still goes


@pytest.fixture
def linked_gate(tmp_path: Path, monkeypatch):
    """The writer gate with its committing bundle kb-a linked in from outside the root, as
    production's bundle is on the phase 2 writer."""
    gate = Gate(tmp_path, monkeypatch)
    real = tmp_path / "home" / "kb-a"
    real.parent.mkdir()
    gate.writer.rename(real)
    gate.writer.symlink_to(real, target_is_directory=True)
    gate.real = real.resolve()
    yield gate
    gate.close()


def test_the_gate_commits_a_changeset_to_a_linked_bundle(linked_gate) -> None:
    gate, real = linked_gate, linked_gate.real
    # The registry hands out the real directory, which owns its Git repository: the predicate of
    # the curate, audit, revert and recovery Git-root checks.
    assert gate.appmod._registry()["kb-a"] == real
    assert curate._repo_root(real).resolve() == real
    head = gate.head()

    dry = gate.post(gate.request(), dry_run=True)
    assert dry.status_code == 200 and dry.json()["status"] == "would_apply", dry.text
    assert gate.remote_head() == head and not (real / ".okf" / "jobs").exists()

    response = gate.post(gate.request())
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "done" and job["git"]["pushed"] is True
    assert job["commit"] == gate.remote_head() == git(real, "rev-parse", "HEAD") != head
    assert git(real, "status", "--porcelain") == ""
    receipt = json.loads((real / ".okf" / "jobs" / f"{job['id']}.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "done"
    wait_for(lambda: gate.audits == [job["id"]])  # its Codex audit queues as for any committing bundle

    published = gate.client.get("/workspace", params={"bundle": "kb-a"}, headers=gate.headers())
    assert published.status_code == 200 and published.headers["X-AIWiki-Revision"] == job["commit"]


def test_a_symlinked_concept_in_a_linked_bundle_is_refused(linked_gate) -> None:
    gate, real = linked_gate, linked_gate.real
    other = clone(gate.remote, gate.tmp / "other")
    os.symlink(os.path.basename(METRIC), other / "metrics" / "alias.md")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "a symlinked concept")
    git(other, "push", "-q", "origin", "main")
    git(real, "fetch", "-q")  # a dry-run judges the writer's origin/main
    published = git(other, "rev-parse", "HEAD")
    request = gate.request({"path": "metrics/alias.md", "op": "put", "base": None, "content": concept("Alias")})

    judged = gate.post(request, dry_run=True)
    committed = gate.post(request)

    assert judged.status_code == 422 and judged.json()["errors"][0]["code"] == "path_forbidden"
    assert committed.status_code >= 400 and committed.json()["status"] in ("failed", "rejected")
    assert gate.remote_head() == published and git(real, "status", "--porcelain") == ""


class _Killed(BaseException):
    """The process dies here: not even the job's own failure handling runs."""


def test_audit_revert_and_recovery_commit_to_a_linked_bundle(linked_gate, monkeypatch) -> None:
    """The Git-root checks of audit, revert and restart recovery pass on the registry's path."""
    gate = linked_gate
    bundle = gate.appmod._registry()["kb-a"]
    changeset = gate.post(gate.request()).json()
    assert changeset["status"] == "done", changeset
    wait_for(lambda: gate.audits == [changeset["id"]])  # the worker is idle again

    # The Codex audit, with a stub reviewer that verifies the changeset's concept.
    verdict = json.dumps({"verified": [METRIC], "unverified": [], "corrected": []})
    reviewed = subprocess.CompletedProcess([], 0, stdout=f"Reviewed.\n\n```json\n{verdict}\n```\n", stderr="")
    monkeypatch.setattr(curate, "_agent_process", lambda _command, **_kw: reviewed)
    job = I.new_audit_job(bundle, changeset["id"], changeset["concept_files"])
    audit_run(bundle, changeset["id"], I.job_path(bundle, job["id"]))
    audited = I.read_job(bundle, job["id"])
    assert (audited["status"], audited["audit"]["status"], audited["git"]["pushed"]) == ("done", "passed", True)
    assert audited["commit"] == gate.remote_head()

    # A revert killed right after its push is reconciled from the pushed commit on restart.
    reverting = I.new_revert_job(bundle, principal="human:owner", actor="human:owner", run=None, reason=None,
                                 selection={"changeset": changeset["id"]},
                                 changesets=[{"id": changeset["id"], "commit": changeset["commit"],
                                              "principal": OPERATOR}])
    real_git = curate._git

    def killed_after_push(root, *args, **kwargs):
        result = real_git(root, *args, **kwargs)
        if args[:1] == ("push",):
            raise _Killed
        return result

    monkeypatch.setattr(curate, "_git", killed_after_push)
    with pytest.raises(_Killed):
        revert.run(bundle, I.job_path(bundle, reverting["id"]))
    monkeypatch.setattr(curate, "_git", real_git)

    assert worker.recover(list(gate.appmod._registry().values())) is True  # as the service starts
    recovered = I.read_job(bundle, reverting["id"])
    assert (recovered["status"], recovered["recovered"]) == ("done", "remote_contains_commit"), recovered
    assert recovered["commit"] == gate.remote_head() == git(bundle, "rev-parse", "HEAD")
    assert git(bundle, "status", "--porcelain") == ""


def test_an_ingest_to_a_linked_bundle_queues_its_job_in_the_real_directory(linked_gate, monkeypatch) -> None:
    gate, real = linked_gate, linked_gate.real
    ran = []
    monkeypatch.setattr(worker.curate, "run", lambda bundle, source, job_path: ran.append((bundle, source, job_path)))

    response = gate.client.post("/ingest", params={"bundle": "kb-a"}, json={"text": "# Note\n\nfacts\n"},
                                headers=gate.headers())
    assert response.status_code == 200, response.text
    job = response.json()
    worker._q.join()

    assert ran == [(real, job["source"], real / ".okf" / "jobs" / f"{job['id']}.json")]
    assert (real / job["source"]).is_file()
