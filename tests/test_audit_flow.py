"""Adversarial audit API/runtime: scope, trust promotion, idempotency and failures."""
from __future__ import annotations

import importlib
import json
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

from aiwiki.runtime import audit, curate
from aiwiki.service import ingest as I


def test_audit_prompt_leaves_bookkeeping_to_the_service() -> None:
    prompt = audit.AUDIT_PROMPT.format(parent_job="p", source="sources/s.md.source", concepts="- a.md")
    assert "never edit `verified`, `generated`, `status`, or `sources`" in prompt
    assert "Never move text between the body and the frontmatter" in prompt
    assert "leave its file byte-for-byte unchanged" in prompt
    assert '{"verified": ["<path>"], "unverified": ["<path>"], "corrected": ["<path>"]}' in prompt
    assert "missing or malformed verdict leaves every scoped concept unverified" in prompt
    # No timestamp for the reviewer to copy: time is stamped by the service.
    assert "{now}" not in audit.AUDIT_PROMPT and "Trusted timestamp" not in prompt


AUTH = {"Authorization": "Bearer testtok"}
AUDIT_NOW = "2026-08-13T01:00:00Z"


def _verdict(verified=(), unverified=(), corrected=()) -> str:
    """A reviewer's final message ending with the machine-readable verdict."""
    data = {"verified": list(verified), "unverified": list(unverified), "corrected": list(corrected)}
    return "Reviewed every scoped concept.\n\n```json\n" + json.dumps(data) + "\n```\n"


def _frontmatter(path: Path) -> dict:
    return audit.parse_doc(path)[0]


def _concept(*, verified: bool = False) -> str:
    fm = {
        "type": "Feature",
        "title": "Release claim",
        "description": "A claim under review",
        "tags": ["demo"],
        "status": "draft",
        "generated": {"by": "process:ai-wiki-curator", "at": "2026-08-13T00:00:00Z"},
        "sources": [{"id": "release-source", "resource": "/sources/release.md.source"}],
    }
    if verified:
        fm["status"] = "stable"
        fm["verified"] = [{"by": audit.AUDITOR, "at": "2026-08-13T01:00:00Z"}]
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n# Summary\n\nThe feature was merged.\n"


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "kb"
    (bundle / ".okf" / "jobs").mkdir(parents=True)
    (bundle / "features").mkdir()
    (bundle / "sources").mkdir()
    (bundle / "features" / "release.md").write_text(_concept(), encoding="utf-8")
    source = bundle / "sources" / "release.md.source"
    source.write_text("The code was merged; release is unknown.\n", encoding="utf-8")
    _source_rel, sha = "sources/inbox/release.md.source", __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    parent = {
        "id": "ingest1", "kind": "ingest", "status": "done", "source": _source_rel,
        "sha256": sha, "validation": {"status": "passed", "error_count": 0},
        "concept_files": ["features/release.md"], "changed_files": ["features/release.md"],
    }
    I.save_job(bundle, parent)
    return bundle


def _git_bundle(tmp_path: Path) -> Path:
    bundle = _bundle(tmp_path)
    (bundle / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-b", "main", str(bundle)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(bundle), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(bundle), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(bundle), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(bundle), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return bundle


def _client(bundle: Path, monkeypatch):
    monkeypatch.setenv("AIWIKI_BUNDLE", str(bundle))
    monkeypatch.delenv("AIWIKI_BUNDLES", raising=False)
    monkeypatch.setenv("AIWIKI_TOKEN", "testtok")
    monkeypatch.setenv("AIWIKI_CURATE", "auto")
    from aiwiki.service import app as appmod
    importlib.reload(appmod)
    from fastapi.testclient import TestClient
    monkeypatch.setattr(appmod.worker, "ensure_started", lambda: None)
    submitted = []
    monkeypatch.setattr(appmod.worker, "submit_audit", lambda *args: submitted.append(args))
    return TestClient(appmod.app), submitted


def test_nested_bundle_audit_fails_before_agent_or_git_mutation(
    tmp_path: Path, monkeypatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    bundle = _bundle(repository)
    sibling = repository / "sibling.txt"
    sibling.write_bytes(b"sibling sentinel")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    calls: list[str] = []

    def unexpected(*args, **kwargs):
        calls.append("unexpected")
        raise AssertionError("nested bundle guard must run before Agent or Git mutation")

    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(audit.curate, "_repo_root", lambda _bundle: repository)
    monkeypatch.setattr(audit.curate, "_exclude_inbox", unexpected)
    monkeypatch.setattr(audit.curate, "_working_files", unexpected)
    monkeypatch.setattr(audit.curate, "_git", unexpected)
    monkeypatch.setattr(audit.curate, "_rollback_git", unexpected)
    monkeypatch.setattr(curate, "_agent_process", unexpected)

    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["audit"]["status"] == "failed"
    assert result["validation"] == {
        "status": "not_run",
        "reason": "nested bundle write is not supported",
    }
    assert result["error"] == "writer requires the bundle to be the Git repository root"
    assert calls == []
    assert sibling.read_bytes() == b"sibling sentinel"


def test_audit_endpoint_is_idempotent_and_uses_jobs_route(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    client, submitted = _client(bundle, monkeypatch)

    first = client.post("/jobs/ingest1/audit", headers=AUTH)
    assert first.status_code == 200
    first_job = first.json()
    assert first_job["kind"] == "audit" and first_job["parent_job"] == "ingest1"
    assert first_job["concept_files"] == ["features/release.md"]
    assert first_job["deduplicated"] is False and len(submitted) == 1

    second_job = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    assert second_job["id"] == first_job["id"] and second_job["deduplicated"] is True
    assert len(submitted) == 1
    assert client.get(f"/jobs/{first_job['id']}", headers=AUTH).json()["parent_job"] == "ingest1"


def test_audit_endpoint_creates_new_attempt_after_technical_failure(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    client, submitted = _client(bundle, monkeypatch)

    first = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    failed = I.read_job(bundle, first["id"])
    failed.update({"status": "failed", "error": "temporary reviewer outage"})
    failed["audit"] = {
        "status": "failed",
        "verified_concepts": [],
        "unverified_concepts": ["features/release.md"],
        "corrected_concepts": [],
    }
    I.save_job(bundle, failed)

    retry = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    assert retry["id"] != first["id"]
    assert retry["status"] == "queued" and retry["deduplicated"] is False
    assert len(submitted) == 2

    duplicate = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    assert duplicate["id"] == retry["id"] and duplicate["deduplicated"] is True
    assert len(submitted) == 2
    assert I.read_job(bundle, first["id"])["status"] == "failed"


def test_audit_endpoint_rejects_noncompleted_ingest(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    parent = I.read_job(bundle, "ingest1")
    parent["status"] = "running"
    I.save_job(bundle, parent)
    client, submitted = _client(bundle, monkeypatch)
    response = client.post("/jobs/ingest1/audit", headers=AUTH)
    assert response.status_code == 409 and "must be done" in response.json()["detail"]
    assert submitted == []


def test_audit_with_no_changed_concepts_is_immediate_idempotent_pass(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    parent = I.read_job(bundle, "ingest1")
    parent["concept_files"] = []
    parent["changed_files"] = ["sources/release.md.source"]
    I.save_job(bundle, parent)
    client, submitted = _client(bundle, monkeypatch)

    first = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    assert first["status"] == "done" and first["reason"] == "no_concepts_to_audit"
    assert first["audit"] == {
        "status": "passed",
        "verified_concepts": [],
        "unverified_concepts": [],
        "corrected_concepts": [],
    }
    assert submitted == []
    second = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    assert second["id"] == first["id"] and second["deduplicated"] is True


def _post_audit_while_mutating(client, appmod, params=None):
    entered = threading.Event()
    release = threading.Event()

    def hold_mutation() -> None:
        with appmod.worker.serialized_mutation():
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=hold_mutation)
    thread.start()
    assert entered.wait(5)
    try:
        response = client.post("/jobs/ingest1/audit", headers=AUTH)
        status = client.get(f"/jobs/{response.json().get('id', 'ingest1')}", headers=AUTH)
    finally:
        release.set()
        thread.join(timeout=5)
    return response, status


def _audit_jobs(bundle: Path) -> list[dict]:
    return [
        job for job in (json.loads(path.read_text(encoding="utf-8"))
                        for path in (bundle / ".okf" / "jobs").glob("*.json"))
        if job.get("kind") == "audit"
    ]


def test_audit_endpoint_enqueues_during_bundle_mutation(tmp_path: Path, monkeypatch) -> None:
    """A long agent pass must not turn POST /audit or a status read into a 503 (CUR-07)."""
    bundle = _bundle(tmp_path)
    client, submitted = _client(bundle, monkeypatch)
    from aiwiki.service import app as appmod

    response, status = _post_audit_while_mutating(client, appmod)

    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "queued" and job["deduplicated"] is False
    assert job["concept_files"] == ["features/release.md"]
    assert status.status_code == 200 and status.json()["id"] == job["id"]
    assert len(submitted) == 1
    assert [audit_job["id"] for audit_job in _audit_jobs(bundle)] == [job["id"]]
    # The queued attempt is idempotent like any other.
    again = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    assert again["id"] == job["id"] and again["deduplicated"] is True


def test_audit_endpoint_during_mutation_keeps_no_concept_and_legacy_gates(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    parent = I.read_job(bundle, "ingest1")
    parent.pop("concept_files")
    I.save_job(bundle, parent)
    client, submitted = _client(bundle, monkeypatch)
    from aiwiki.service import app as appmod

    # An unscoped legacy receipt needs the live tree to resolve scope: nothing is created.
    response, _status = _post_audit_while_mutating(client, appmod)
    assert response.status_code == 409 and "in progress" in response.json()["detail"]
    assert submitted == [] and _audit_jobs(bundle) == []

    # A receipt that declares no concepts is a tree-independent no-op pass.
    parent["concept_files"] = []
    I.save_job(bundle, parent)
    response, _status = _post_audit_while_mutating(client, appmod)
    assert response.status_code == 200
    assert response.json()["status"] == "done" and response.json()["reason"] == "no_concepts_to_audit"
    assert submitted == []


def test_queued_audit_rechecks_declared_scope_on_the_live_tree(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "features" / "other.md").write_text(_concept(), encoding="utf-8")
    parent = I.read_job(bundle, "ingest1")
    parent["concept_files"] = ["features/other.md", "features/release.md"]
    I.save_job(bundle, parent)
    job = I.new_audit_job(bundle, "ingest1", parent["concept_files"])
    (bundle / "features" / "other.md").unlink()  # a later pass removed a declared concept
    monkeypatch.setenv("AIWIKI_GIT", "off")

    def unexpected(*args, **kwargs):
        raise AssertionError("a scope mismatch must fail before the reviewer runs")

    monkeypatch.setattr(curate, "_agent_process", unexpected)
    audit.run(bundle, "ingest1", I.job_path(bundle, job["id"]))

    result = I.read_job(bundle, job["id"])
    assert result["status"] == "failed"
    assert result["error"] == "ingest audit scope is missing or invalid"
    assert result["failure"]["class"] == "input" and result["failure"]["retryable"] is False


def test_audit_endpoint_rejects_missing_declared_concept_scope(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "features" / "release.md").unlink()
    client, submitted = _client(bundle, monkeypatch)

    response = client.post("/jobs/ingest1/audit", headers=AUTH)

    assert response.status_code == 409
    assert "missing or invalid" in response.json()["detail"]
    assert "features/release.md" in response.json()["detail"]
    assert submitted == []
    assert not any(
        json.loads(path.read_text(encoding="utf-8")).get("kind") == "audit"
        for path in (bundle / ".okf" / "jobs").glob("*.json")
    )


def _run_runtime(tmp_path: Path, monkeypatch, *, verify: bool, validate_errors=None) -> dict:
    bundle = _bundle(tmp_path)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: validate_errors or [])

    def fake_agent(*args, **kwargs):
        rel = "features/release.md"
        message = _verdict(verified=[rel]) if verify else _verdict(unverified=[rel])
        return subprocess.CompletedProcess(args[0], 0, stdout=message, stderr="")

    monkeypatch.setattr(curate, "_agent_process", fake_agent)
    audit.run(bundle, "ingest1", path)
    return json.loads(path.read_text(encoding="utf-8"))


def test_runtime_passed_when_all_scoped_concepts_are_machine_verified(tmp_path: Path, monkeypatch) -> None:
    job = _run_runtime(tmp_path, monkeypatch, verify=True)
    assert job["status"] == "done"
    assert job["audit"] == {
        "status": "passed",
        "verified_concepts": ["features/release.md"],
        "unverified_concepts": [],
        "corrected_concepts": [],
    }
    assert job["validation"] == {"status": "passed", "error_count": 0}
    assert job["parent_job"] == "ingest1" and job["commit"] is None
    assert job["closeout"]["log"] == "log.md"
    assert "index.md" in job["closeout"]["indexes"]
    assert job["verdict"]["status"] == "valid"
    frontmatter = _frontmatter(tmp_path / "kb" / "features" / "release.md")
    assert frontmatter["status"] == "stable"
    assert frontmatter["verified"] == [{"by": audit.AUDITOR, "at": audit._instant(AUDIT_NOW)}]


@pytest.mark.parametrize("verify", [True, False], ids=["passed", "needs_attention"])
def test_durable_audit_receipt_survives_stale_missing_and_busy_reads(
    tmp_path: Path, monkeypatch, verify: bool,
) -> None:
    """Checkpoint the audited commit, never the mirror or a subsequent working tree."""
    from aiwiki.engine.document import concept_metadata
    from aiwiki.engine.validate import parse_doc

    bundle = _git_bundle(tmp_path)
    mirror = _bundle(tmp_path / "mirror")
    (mirror / "index.md").write_text('---\nokf_version: "0.2"\n---\n', encoding="utf-8")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    assert curate._git(bundle, "remote", "add", "origin", str(remote)).returncode == 0
    assert curate._git(bundle, "push", "-u", "origin", "main").returncode == 0
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(curate, "_now", lambda: AUDIT_NOW)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])

    def fake_agent(command, **kwargs):
        rel = "features/release.md"
        message = _verdict(verified=[rel]) if verify else _verdict(unverified=[rel])
        return subprocess.CompletedProcess(command, 0, stdout=message, stderr="")

    monkeypatch.setattr(curate, "_agent_process", fake_agent)
    audit.run(bundle, "ingest1", I.job_path(bundle, job["id"]))
    receipt = I.read_job(bundle, job["id"])
    assert receipt["status"] == "done"
    assert receipt["validation"]["status"] == "passed"
    assert receipt["audit"]["status"] == ("passed" if verify else "needs_attention")
    assert receipt["git"]["committed"] is True and receipt["git"]["pushed"] is True
    assert receipt["commit"] == receipt["git"]["commit"]
    assert curate._git(remote, "rev-parse", "refs/heads/main").stdout.strip() == receipt["commit"]
    metadata = concept_metadata(parse_doc(bundle / "features/release.md")[0])
    assert metadata["status"] == "stable"
    assert metadata["verification_current"] is verify

    client, _submitted = _client(bundle, monkeypatch)
    from aiwiki.service import app as appmod
    monkeypatch.setattr(appmod, "_registry", lambda: {"writer": bundle, "reader": mirror})
    params = {"path": "features/release.md", "bundle": "reader"}
    stale = client.get("/cat", params=params, headers=AUTH)
    assert stale.json()["metadata"]["status"] == "draft"
    (mirror / "features/release.md").unlink()
    assert client.get("/cat", params=params, headers=AUTH).status_code == 404

    # Even the writer's latest tree is not the immutable revision this receipt describes.
    (bundle / "features/release.md").write_text(_concept(), encoding="utf-8")
    before = I.job_path(bundle, job["id"]).read_bytes()
    with appmod.worker.serialized_mutation():
        assert client.get("/cat", params=params, headers=AUTH).status_code == 503
        for _ in range(2):
            result = client.get(f"/jobs/{job['id']}", params={"bundle": "writer"}, headers=AUTH)
            assert result.status_code == 200
            assert result.json() == receipt
    assert I.job_path(bundle, job["id"]).read_bytes() == before


def test_audit_push_failure_is_not_a_successful_receipt(tmp_path: Path, monkeypatch) -> None:
    bundle = _git_bundle(tmp_path)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    monkeypatch.setattr(
        curate, "_agent_process",
        lambda command, **kw: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(curate, "_has_remote", lambda _root: True)
    monkeypatch.setattr(curate, "_commit_and_push", lambda *args: {"committed": True, "pushed": False})

    audit.run(bundle, "ingest1", I.job_path(bundle, job["id"]))

    result = I.read_job(bundle, job["id"])
    assert result["status"] == "failed"
    assert result["error"] == "audit git commit/push failed"


def test_audit_closeout_runs_after_agent_scope_gate_and_logs_scoped_concepts(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)
    events = []

    def fake_agent(*args, **kwargs):
        events.append("agent")
        (bundle / "features" / "release.md").write_text(_concept(verified=True), encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, stdout="reviewed", stderr="")

    def fake_closeout(_bundle, parent, concepts):
        events.append("closeout")
        assert parent == "ingest1" and concepts == ["features/release.md"]
        (bundle / "index.md").write_text('---\nokf_version: "0.2"\n---\n', encoding="utf-8")
        (bundle / "log.md").write_text(
            "# Update Log\n\n## 2026-08-13\n"
            "* **Audit**: Audited ingest ingest1 — files: features/release.md\n",
            encoding="utf-8",
        )
        return {"indexes": ["index.md"], "log": "log.md", "missing_index_descriptions": []}

    monkeypatch.setattr(curate, "_agent_process", fake_agent)
    monkeypatch.setattr(audit, "_deterministic_closeout", fake_closeout)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "done"
    assert events == ["agent", "closeout"]
    assert set(result["changed_files"]) == {"features/release.md", "index.md", "log.md"}


def test_audit_uses_same_sandboxed_codex_boundary(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    captured = []

    def fake_agent(command, **kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="reviewed", stderr="")

    monkeypatch.setattr(curate, "_agent_process", fake_agent)
    audit.run(bundle, "ingest1", path)
    command = captured[0]
    assert command[0] == curate.AGENT_BIN
    assert all(index < command.index("exec") for index, arg in enumerate(command)
               if arg in ("--config", "--disable"))
    assert command[command.index("--model") + 1] == curate.AGENT_MODEL
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=false" in command
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in command
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert set(curate._DISABLED_CODEX_FEATURES).issubset(command)


@pytest.mark.parametrize("outcome", ["normal", "nonzero", "timeout"])
def test_git_audit_preserves_concurrent_operational_state_for_every_agent_exit(
    tmp_path: Path, monkeypatch, outcome: str,
) -> None:
    bundle = _git_bundle(tmp_path)
    other_job = bundle / ".okf" / "jobs" / "other.json"
    original = b'{"id":"other","status":"done"}\n'
    other_job.write_bytes(original)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    concurrent = b'{"id":"other","status":"queued"}\n'
    concurrent_inbox = bundle / "sources" / "inbox" / "concurrent.md.source"
    real_run = subprocess.run

    def concurrent_enqueue(command, *args, **kwargs):
        if command[0] != curate.AGENT_BIN:
            return real_run(command, *args, **kwargs)
        other_job.write_bytes(concurrent)
        concurrent_inbox.parent.mkdir(parents=True, exist_ok=True)
        concurrent_inbox.write_bytes(b"concurrent evidence")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, audit.TIMEOUT_S)
        return subprocess.CompletedProcess(
            command,
            0 if outcome == "normal" else 1,
            stdout="reviewed" if outcome == "normal" else "",
            stderr="" if outcome == "normal" else "failed",
        )

    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(curate, "_agent_process", concurrent_enqueue)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    monkeypatch.setattr(
        audit,
        "_deterministic_closeout",
        lambda *_args: {"indexes": [], "log": "log.md", "missing_index_descriptions": []},
    )
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == ("done" if outcome == "normal" else "failed")
    assert other_job.read_bytes() == concurrent
    assert concurrent_inbox.read_bytes() == b"concurrent evidence"
    assert real_run(
        ["git", "-C", str(bundle), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


def test_audit_timeout_restores_git_config_and_hook_before_any_git_command(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _git_bundle(tmp_path)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    config = bundle / ".git" / "config"
    original_config = config.read_bytes()
    hook = bundle / ".git" / "hooks" / "pre-commit"
    executed = tmp_path / "hook-executed"
    real_git = curate._git
    attacked = False
    safe_git_calls = 0

    def metadata_attack(command, *args, **kwargs):
        nonlocal attacked, safe_git_calls
        if command[0] == curate.AGENT_BIN:
            config.write_text("[core]\n\thooksPath = .git/hooks\n", encoding="utf-8")
            hook.write_text(f"#!/bin/sh\ntouch {executed}\n", encoding="utf-8")
            hook.chmod(0o755)
            attacked = True
            raise subprocess.TimeoutExpired(command, audit.TIMEOUT_S)
        pytest.fail("only the Codex command belongs in _agent_process")

    def guarded_git(root, *args, **kwargs):
        nonlocal safe_git_calls
        if attacked:
            safe_git_calls += 1
            assert config.read_bytes() == original_config
            assert not hook.exists()
        return real_git(root, *args, **kwargs)

    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(curate, "_agent_process", metadata_attack)
    monkeypatch.setattr(curate, "_git", guarded_git)
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["validation"]["reason"] == "Git metadata integrity violation"
    assert ".git/config" in result["out_of_scope_files"]
    assert ".git/hooks/pre-commit" in result["out_of_scope_files"]
    assert safe_git_calls > 0
    assert config.read_bytes() == original_config
    assert not hook.exists()
    assert not executed.exists()


def test_runtime_needs_attention_is_a_successful_job(tmp_path: Path, monkeypatch) -> None:
    job = _run_runtime(tmp_path, monkeypatch, verify=False)
    assert job["status"] == "done"
    assert job["audit"]["status"] == "needs_attention"
    assert job["audit"]["unverified_concepts"] == ["features/release.md"]
    assert job["validation"]["status"] == "passed"
    concept = tmp_path / "kb" / "features" / "release.md"
    frontmatter = yaml.safe_load(concept.read_text(encoding="utf-8").split("---", 2)[1])
    assert frontmatter["status"] == "stable"
    assert "verified" not in frontmatter


def test_unsupported_audit_preserves_historical_auditor_verification(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    frontmatter = yaml.safe_load(_concept()[4:_concept().find("\n---\n", 4)])
    historical = {"by": audit.AUDITOR, "at": "2026-08-12T23:00:00Z"}
    frontmatter["verified"] = [historical]
    original = (
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False)
        + "---\n# Summary\n\nThe feature was merged.\n"
    )
    concept.write_text(original, encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    monkeypatch.setattr(
        curate,
        "_agent_process",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="unsupported", stderr="",
        ),
    )

    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "done"
    assert result["audit"]["status"] == "needs_attention"
    after_text = concept.read_text(encoding="utf-8")
    after = yaml.safe_load(after_text[4:after_text.find("\n---\n", 4)])
    assert after["status"] == "stable"
    assert after["verified"] == [historical]


def test_runtime_validation_failure_is_technical_failure(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    original = (bundle / "features" / "release.md").read_text(encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: ["features/release.md: bad"])

    def bad_review(*args, **kwargs):
        (bundle / "features" / "release.md").write_text(_concept(verified=True), encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, stdout="reviewed", stderr="")

    monkeypatch.setattr(curate, "_agent_process", bad_review)
    audit.run(bundle, "ingest1", path)
    job = json.loads(path.read_text(encoding="utf-8"))
    assert job["status"] == "failed" and job["audit"]["status"] == "failed"
    assert job["validation"]["status"] == "failed"
    assert job["validation"]["errors"] == ["features/release.md: bad"]
    assert (bundle / "features" / "release.md").read_text(encoding="utf-8") == original


@pytest.mark.parametrize("git_enabled", [False, True])
def test_audit_normalizes_reviewer_mixed_verification_indentation(
    tmp_path: Path, monkeypatch, git_enabled: bool,
) -> None:
    """9de6461bab99: an indentless event appended to an indented list broke the YAML."""
    bundle = _git_bundle(tmp_path) if git_enabled else _bundle(tmp_path)
    concept = bundle / "features/release.md"
    historical = f"  - {{by: {audit.AUDITOR}, at: '2026-08-12T01:00:00Z'}}"
    original = concept.read_text(encoding="utf-8").replace(
        "\n---\n# Summary", f"\nverified:\n{historical}\n---\n# Summary",
    )
    concept.write_text(original, encoding="utf-8")
    if git_enabled:
        curate._git(bundle, "add", ".")
        curate._git(bundle, "commit", "-m", "retain indented verification history")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "on" if git_enabled else "off")
    monkeypatch.setattr(curate, "_now", lambda: AUDIT_NOW)
    monkeypatch.setattr(audit, "validate_bundle", lambda *args: [])

    def malformed_review(*args, **kwargs):
        malformed = original.replace("status: draft", "status: stable").replace(
            "\n---\n# Summary",
            f"\n- {{by: {audit.AUDITOR}, at: '{AUDIT_NOW}'}}\n---\n# Summary",
        )
        concept.write_text(malformed, encoding="utf-8")
        return subprocess.CompletedProcess(
            args[0], 0, stdout=_verdict(verified=["features/release.md"]), stderr="",
        )

    monkeypatch.setattr(curate, "_agent_process", malformed_review)
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "done" and result["audit"]["status"] == "passed"
    assert result["validation"] == {"status": "passed", "error_count": 0}
    assert result["deterministic_repairs"] == {
        "features/release.md": ["restored service-owned verification history"],
    }
    # Only the service's event is appended, in the list's own indentation and quoting.
    assert concept.read_text(encoding="utf-8") == original.replace("status: draft", "status: stable").replace(
        historical, f"{historical}\n  - {{by: {audit.AUDITOR}, at: '{AUDIT_NOW}'}}",
    )
    if git_enabled:
        assert result["git"]["committed"] is True
        assert curate._git(bundle, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("git_enabled", [False, True])
def test_audit_invalid_yaml_outside_bookkeeping_reports_path_and_rolls_back(
    tmp_path: Path, monkeypatch, git_enabled: bool,
) -> None:
    bundle = _git_bundle(tmp_path) if git_enabled else _bundle(tmp_path)
    concept = bundle / "features/release.md"
    original = concept.read_text(encoding="utf-8")
    base_revision = curate._git(bundle, "rev-parse", "HEAD").stdout.strip() if git_enabled else None
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "on" if git_enabled else "off")
    monkeypatch.setattr(curate, "_now", lambda: AUDIT_NOW)
    closeout = []
    monkeypatch.setattr(audit, "validate_bundle", lambda *args: closeout.append("validate"))

    def malformed_review(*args, **kwargs):
        concept.write_text(original.replace("- demo", "- demo\n- [broken"), encoding="utf-8")
        return subprocess.CompletedProcess(
            args[0], 0, stdout=_verdict(verified=["features/release.md"]), stderr="",
        )

    monkeypatch.setattr(curate, "_agent_process", malformed_review)
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed" and result["phase"] == "rolled_back"
    assert result["validation"]["status"] == "failed"
    assert result["validation"]["error_count"] == 1
    assert result["validation"]["errors"][0].startswith("features/release.md: invalid YAML frontmatter:")
    assert result["audit"]["verified_concepts"] == []
    assert not result.get("git", {}).get("committed")
    assert concept.read_text(encoding="utf-8") == original
    assert closeout == []
    if git_enabled:
        assert curate._git(bundle, "rev-parse", "HEAD").stdout.strip() == base_revision
        assert curate._git(bundle, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("indent", ["", "  "])
def test_audit_appends_service_event_in_existing_list_style(tmp_path: Path, monkeypatch, indent: str) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "index.md").write_text('---\nokf_version: "0.2"\n---\n', encoding="utf-8")
    concept = bundle / "features/release.md"
    historical = f"{indent}- {{by: {audit.AUDITOR}, at: '2026-08-12T01:00:00Z'}}"
    original = concept.read_text(encoding="utf-8").replace(
        "\n---\n# Summary", f"\nverified:\n{historical}\n---\n# Summary",
    )
    concept.write_text(original, encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(curate, "_now", lambda: AUDIT_NOW)
    monkeypatch.setattr(
        curate, "_agent_process",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=_verdict(verified=["features/release.md"]), stderr="",
        ),
    )
    audit.run(bundle, "ingest1", path)
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "done" and result["audit"]["status"] == "passed"
    assert result["validation"]["status"] == "passed"
    assert "deterministic_repairs" not in result
    assert f"{historical}\n{indent}- {{by: {audit.AUDITOR}, at: '{AUDIT_NOW}'}}\n---" in (
        concept.read_text(encoding="utf-8")
    )


def test_audit_preflight_rejects_bundle_symlink_without_starting_agent(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    outside_existing = tmp_path / "existing.txt"
    outside_existing.write_text("existing sentinel", encoding="utf-8")
    existing_link = bundle / "existing-link"
    existing_link.symlink_to(outside_existing)
    outside_new = tmp_path / "new.txt"
    outside_new.write_text("new sentinel", encoding="utf-8")
    new_link = bundle / "new-link"
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    called = []

    def failed_agent(*args, **kwargs):
        called.append(True)
        outside_existing.write_text("tampered", encoding="utf-8")
        new_link.symlink_to(outside_new)
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="failed")

    monkeypatch.setattr(curate, "_agent_process", failed_agent)
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["audit"]["status"] == "failed"
    assert result["symlink_paths"] == ["existing-link"]
    assert "contains symlinks" in result["error"]
    assert called == []
    assert existing_link.is_symlink()
    assert outside_existing.read_text(encoding="utf-8") == "existing sentinel"
    assert not new_link.exists() and not new_link.is_symlink()
    assert outside_new.read_text(encoding="utf-8") == "new sentinel"


def test_no_git_audit_rejects_successful_agent_out_of_scope_edit(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    purpose = bundle / "purpose.md"
    purpose.write_text("original", encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")

    def malicious_agent(*args, **kwargs):
        purpose.write_text("tampered", encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, stdout="reviewed", stderr="")

    monkeypatch.setattr(curate, "_agent_process", malicious_agent)
    audit.run(bundle, "ingest1", path)
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["out_of_scope_files"] == ["purpose.md"]
    assert purpose.read_text(encoding="utf-8") == "original"


def _review(bundle: Path, monkeypatch, edit, message: str, clock: dict | None = None) -> dict:
    """Run one no-Git audit whose reviewer applies ``edit`` to the scoped concept."""
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    clock = clock if clock is not None else {"now": AUDIT_NOW}
    monkeypatch.setattr(audit.curate, "_now", lambda: clock["now"])
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])

    def reviewer(*args, **kwargs):
        edit(bundle / "features" / "release.md")
        clock["now"] = clock.get("finish", clock["now"])
        return subprocess.CompletedProcess(args[0], 0, stdout=message, stderr="")

    monkeypatch.setattr(curate, "_agent_process", reviewer)
    audit.run(bundle, "ingest1", path)
    return json.loads(path.read_text(encoding="utf-8"))


def _rewrite(concept: Path, change) -> None:
    text = concept.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text[4:text.find("\n---\n", 4)])
    body = change(frontmatter)
    concept.write_text(
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n"
        + (body if isinstance(body, str) else "# Summary\n\nThe feature was merged.\n"),
        encoding="utf-8",
    )


def test_audit_stamps_generation_for_substantive_correction(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    result = _review(
        bundle, monkeypatch,
        lambda path: path.write_text(
            path.read_text(encoding="utf-8").replace("The feature was merged.", "The feature may be merged."),
            encoding="utf-8",
        ),
        _verdict(verified=["features/release.md"], corrected=["features/release.md"]),
    )
    assert result["status"] == "done" and result["audit"]["status"] == "passed"
    assert result["audit"]["corrected_concepts"] == ["features/release.md"]
    text = concept.read_text(encoding="utf-8")
    # generated keeps its quoted block style; the new verified list uses flow style.
    assert f"generated:\n  by: {audit.AUDITOR}\n  at: '{AUDIT_NOW}'\n" in text
    assert f"verified:\n  - {{by: {audit.AUDITOR}, at: {AUDIT_NOW}}}\n" in text
    assert "may be merged" in text


def test_audit_missing_verdict_is_needs_attention_not_a_failed_job(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    result = _review(
        bundle, monkeypatch,
        lambda path: path.write_text(
            path.read_text(encoding="utf-8").replace("The feature was merged.", "The feature may be merged."),
            encoding="utf-8",
        ),
        "All five scoped concepts are verified.",  # prose only: no machine-readable verdict
    )
    assert result["status"] == "done"
    assert result["validation"]["status"] == "passed"
    assert result["verdict"]["status"] == "missing"
    assert result["audit"] == {
        "status": "needs_attention",
        "verified_concepts": [],
        "unverified_concepts": ["features/release.md"],
        "corrected_concepts": ["features/release.md"],
        "reason": "verdict_missing",
    }
    frontmatter = _frontmatter(concept)
    assert frontmatter["status"] == "stable"
    assert frontmatter["generated"]["by"] == audit.AUDITOR
    assert "verified" not in frontmatter


@pytest.mark.parametrize(
    "message",
    [
        '```json\n{"verified": "features/release.md"}\n```',
        '```json\n{"verified": [1]}\n```',
        "```json\n{not json}\n```",
    ],
)
def test_audit_invalid_verdict_verifies_nothing(tmp_path: Path, monkeypatch, message: str) -> None:
    result = _review(_bundle(tmp_path), monkeypatch, lambda path: None, message)
    assert result["status"] == "done"
    assert result["audit"]["status"] == "needs_attention"
    assert result["verdict"]["status"] in {"invalid", "missing"}
    assert result["audit"]["reason"] == "verdict_" + result["verdict"]["status"]
    assert "verified" not in _frontmatter(tmp_path / "kb" / "features" / "release.md")


def test_audit_endpoint_allows_one_reaudit_after_a_verdict_format_failure(tmp_path: Path, monkeypatch) -> None:
    """A garbled verdict judged no evidence, so it must not be the parent's final audit."""
    bundle = _bundle(tmp_path)
    client, submitted = _client(bundle, monkeypatch)

    def finish(bundle: Path, job_id: str, reason: str | None) -> None:
        job = I.read_job(bundle, job_id)
        job.update({"status": "done", "validation": {"status": "passed", "error_count": 0}})
        job["audit"] = {"status": "needs_attention", "verified_concepts": [],
                        "unverified_concepts": ["features/release.md"], "corrected_concepts": []}
        if reason:
            job["audit"]["reason"] = reason
        I.save_job(bundle, job)

    first = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    finish(bundle, first["id"], "verdict_missing")
    retry = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    assert retry["id"] != first["id"] and retry["deduplicated"] is False and len(submitted) == 2
    finish(bundle, retry["id"], "verdict_invalid")
    # The bound is reached: the newest garbled attempt is now the idempotent result.
    final = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    assert final["id"] == retry["id"] and final["deduplicated"] is True and len(submitted) == 2

    # A judged needs_attention (valid verdict) is final at once.
    judged = _bundle(tmp_path / "judged")
    client, submitted = _client(judged, monkeypatch)
    first = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    finish(judged, first["id"], None)
    again = client.post("/jobs/ingest1/audit", headers=AUTH).json()
    assert again["id"] == first["id"] and again["deduplicated"] is True and len(submitted) == 1


@pytest.mark.parametrize(
    "message",
    [
        # An earlier non-JSON fence must not shift which fence holds the verdict.
        "Report\n```yaml\nstatus: stable\n```\nVerdict:\n```json\n"
        '{"notes": "ok", "verified": ["features/release.md"], "unverified": [], "corrected": []}\n```\n',
        # JSON-looking prose after the fenced verdict does not override it.
        _verdict(verified=["features/release.md"])
        + 'Note: the source config sets `{"verified": false}` for the flag.\n',
        # A null list is an empty list.
        '{"verified": ["features/release.md"], "unverified": null, "corrected": []}',
        # A bare file name that names exactly one scoped concept.
        '```json\n{"verified": ["release.md"]}\n```',
        # An unusable later candidate is skipped, not fatal.
        _verdict(verified=["features/release.md"]) + '```json\n{"verified": "features/release.md"}\n```\n',
    ],
)
def test_verdict_parser_tolerates_surrounding_text(message: str) -> None:
    assert audit._parse_verdict(message, ["features/release.md", "features/other.md"]) == {
        "status": "valid",
        "verified": ["features/release.md"],
        "unverified": ["features/other.md"],
        "corrected": [],
    }


def test_audit_reads_the_verdict_from_the_last_message_file(tmp_path: Path, monkeypatch) -> None:
    """Production reads codex --output-last-message; stdout is only a fallback."""
    bundle = _bundle(tmp_path)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])

    def reviewer(command, **kwargs):
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(_verdict(verified=["features/release.md"]), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=_verdict(unverified=["features/release.md"]),
                                           stderr="")

    monkeypatch.setattr(curate, "_agent_process", reviewer)
    audit.run(bundle, "ingest1", path)
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "done" and result["audit"]["status"] == "passed"
    assert result["verdict"]["verified"] == ["features/release.md"]


def test_audit_verdict_maps_workspace_paths_and_prefers_unverified(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    absolute = "/home/admin/solvely-wiki/features/release.md"  # as written in 71ea85ca9c20's summary
    result = _review(
        bundle, monkeypatch, lambda path: None,
        "Report.\n```json\n" + json.dumps({"verified": [absolute, "features/other.md"]}) + "\n```\n"
        + "Afterthought, still final:\n```\n" + json.dumps({"verified": [absolute], "unverified": []}) + "\n```",
    )
    assert result["audit"]["status"] == "passed"
    assert result["verdict"] == {
        "status": "valid",
        "verified": ["features/release.md"],
        "unverified": [],
        "corrected": [],
    }
    both = _review(
        _bundle(tmp_path / "second"), monkeypatch, lambda path: None,
        _verdict(verified=["features/release.md"], unverified=["./features/release.md"]),
    )
    assert both["audit"]["status"] == "needs_attention"
    assert "verified" not in _frontmatter(tmp_path / "second" / "kb" / "features" / "release.md")


def test_audit_discards_forged_human_verification(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    original = concept.read_text(encoding="utf-8")

    def forge(path: Path) -> None:
        def change(frontmatter: dict) -> None:
            frontmatter["status"] = "stable"
            frontmatter["verified"] = [{"by": "human:owner", "at": "2026-08-13T01:00:00Z"}]
        _rewrite(path, change)

    result = _review(bundle, monkeypatch, forge, "forged")
    assert result["status"] == "done" and result["audit"]["status"] == "needs_attention"
    assert result["deterministic_repairs"] == {
        "features/release.md": ["restored service-owned verification history"],
    }
    assert concept.read_text(encoding="utf-8") == original.replace("status: draft", "status: stable")


def test_audit_keeps_unverified_stable_as_completed_needs_attention(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    original = concept.read_text(encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")

    def unsupported_stable(*args, **kwargs):
        frontmatter = yaml.safe_load(original[4:original.find("\n---\n", 4)])
        frontmatter["status"] = "stable"
        concept.write_text(
            "---\n" + yaml.safe_dump(frontmatter, sort_keys=False)
            + "---\n# Summary\n\nThe feature was merged.\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args[0], 0, stdout="unsupported", stderr="")

    monkeypatch.setattr(curate, "_agent_process", unsupported_stable)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "done"
    assert result["audit"]["status"] == "needs_attention"
    assert result["audit"]["unverified_concepts"] == ["features/release.md"]
    assert "deterministic_repairs" not in result
    assert yaml.safe_load(concept.read_text(encoding="utf-8").split("---", 2)[1])["status"] == "stable"


def test_audit_promotes_leftover_draft_to_stable_without_faking_verification(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    result = _review(bundle, monkeypatch, lambda path: None, _verdict(unverified=["features/release.md"]))
    frontmatter = _frontmatter(concept)
    assert result["status"] == "done"
    assert result["audit"]["status"] == "needs_attention"
    assert "deterministic_repairs" not in result  # status is service-owned, not a repair
    assert frontmatter["status"] == "stable"
    assert "verified" not in frontmatter


def test_audit_keeps_deprecated_concepts_deprecated(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    concept.write_text(
        concept.read_text(encoding="utf-8").replace("status: draft", "status: deprecated"), encoding="utf-8",
    )
    result = _review(bundle, monkeypatch, lambda path: None, _verdict(verified=["features/release.md"]))
    assert result["audit"]["status"] == "passed"
    assert _frontmatter(concept)["status"] == "deprecated"


def test_audit_restores_generation_when_only_provenance_edit_survived(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)

    def provenance_only(path: Path) -> None:
        def change(frontmatter: dict) -> None:
            frontmatter["status"] = "stable"
            frontmatter["generated"] = {"by": audit.AUDITOR, "at": AUDIT_NOW}
            frontmatter["sources"][0]["author"] = "process:guessed"
        _rewrite(path, change)

    result = _review(bundle, monkeypatch, provenance_only, _verdict(verified=["features/release.md"]))
    assert result["status"] == "done"
    assert result["audit"]["status"] == "passed"
    assert result["deterministic_repairs"] == {
        "features/release.md": [
            "restored immutable sources provenance",
            "restored generated after discarded non-substantive edits",
        ]
    }


def test_audit_replaces_future_bookkeeping_with_trusted_time(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"

    def future_review(path: Path) -> None:
        def change(frontmatter: dict) -> None:
            frontmatter["description"] = "A materially corrected claim"
            frontmatter["status"] = "stable"
            frontmatter["generated"] = {"by": audit.AUDITOR, "at": "2099-01-01T00:00:00Z"}
            frontmatter["verified"] = [{"by": audit.AUDITOR, "at": "2099-01-01T00:00:00Z"}]
        _rewrite(path, change)

    result = _review(bundle, monkeypatch, future_review, _verdict(verified=["features/release.md"]))
    assert result["status"] == "done" and result["audit"]["status"] == "passed"
    frontmatter = _frontmatter(concept)
    stamp = audit._instant(AUDIT_NOW)
    assert frontmatter["description"] == "A materially corrected claim"
    assert frontmatter["generated"]["by"] == audit.AUDITOR
    assert audit._instant(frontmatter["generated"]["at"]) == stamp
    assert frontmatter["verified"] == [{"by": audit.AUDITOR, "at": stamp}]


def test_audit_stamps_verification_with_trusted_finish_not_reviewer_time(
    tmp_path: Path, monkeypatch,
) -> None:
    """920d5b3 restamped only a single stale event; the service now writes the event."""
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    human = {"by": "human:owner", "at": "2026-08-13T00:30:00Z"}
    _rewrite(concept, lambda frontmatter: frontmatter.update(verified=[human]))

    def stale_review(path: Path) -> None:
        def change(frontmatter: dict) -> None:
            frontmatter["status"] = "stable"
            frontmatter["verified"].extend([
                {"by": audit.AUDITOR, "at": "2026-08-13T00:50:00Z"},
                {"by": audit.AUDITOR, "at": "2026-08-13T00:51:00Z"},
            ])
        _rewrite(path, change)

    clock = {"now": AUDIT_NOW, "finish": "2026-08-13T01:10:00Z"}
    result = _review(bundle, monkeypatch, stale_review, _verdict(verified=["features/release.md"]), clock)
    assert result["status"] == "done" and result["audit"]["status"] == "passed"
    assert result["deterministic_repairs"] == {
        "features/release.md": ["restored service-owned verification history"],
    }
    verified = _frontmatter(concept)["verified"]
    assert [event["by"] for event in verified] == ["human:owner", audit.AUDITOR]
    assert audit._instant(verified[0]["at"]) == audit._instant(human["at"])
    assert audit._instant(verified[1]["at"]) == audit._instant("2026-08-13T01:10:00Z")


LIVE = Path(__file__).parent / "fixtures" / "live_bundle"
ORPHAN_REL = "experiments/web-landing-page-aio-ab.md"
ORPHAN_AT = "2026-09-19T20:56:59Z"
ORPHAN_LINE = f"  - {{by: {audit.AUDITOR}, at: {ORPHAN_AT}}}"
HISTORY_TAIL = f"  - {{by: {audit.AUDITOR}, at: 2026-09-17T21:18:46Z}}"
ORPHAN_AUDIT_START = "2026-09-23T15:52:00Z"
ORPHAN_AUDIT_FINISH = "2026-09-23T15:55:15Z"


def _orphan_bundle(tmp_path: Path) -> tuple[Path, str]:
    """The pre-audit file of 71ea85ca9c20/e94c8b707aea (bundle 3b5731b, prose redacted)."""
    bundle = tmp_path / "kb"
    (bundle / ".okf" / "jobs").mkdir(parents=True)
    (bundle / "index.md").write_text('---\nokf_version: "0.2"\n---\n# Bundle\n', encoding="utf-8")
    original = (LIVE / ORPHAN_REL).read_text(encoding="utf-8")
    (bundle / ORPHAN_REL).parent.mkdir(parents=True)
    (bundle / ORPHAN_REL).write_text(original, encoding="utf-8")
    frontmatter = yaml.safe_load(original[4:original.find("\n---\n", 4)])
    for source in frontmatter["sources"]:
        path = bundle / source["resource"].lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"placeholder evidence for {source['id']}\n", encoding="utf-8")
    evidence = bundle / frontmatter["sources"][0]["resource"].lstrip("/")
    I.save_job(bundle, {
        "id": "6f4b6f97e4b2", "kind": "ingest", "status": "done",
        "source": "sources/inbox/" + evidence.name,
        "sha256": __import__("hashlib").sha256(evidence.read_bytes()).hexdigest(),
        "validation": {"status": "passed", "error_count": 0},
        "concept_files": [ORPHAN_REL], "changed_files": [ORPHAN_REL],
    })
    return bundle, original


def _lift_orphan(text: str, *, new_event: bool = True, refresh: bool = True, keep_body: bool = False,
                 chronological: bool = False) -> str:
    """Text-level reviewer edits observed or simulated around the body orphan."""
    lifted = [ORPHAN_LINE] + ([f"  - {{by: {audit.AUDITOR}, at: {ORPHAN_AUDIT_START}}}"] if new_event else [])
    if chronological:
        text = text.replace("  - {by: process:ai-wiki-adversarial-audit, at: 2026-09-14T20:50:19Z}\n",
                            "  - {by: process:ai-wiki-adversarial-audit, at: 2026-09-14T20:50:19Z}\n"
                            + ORPHAN_LINE + "\n")
        lifted = lifted[1:]
    appended = "".join(f"{line}\n" for line in lifted)
    text = text.replace(HISTORY_TAIL + "\n---\n", HISTORY_TAIL + "\n" + appended + "---\n")
    if not keep_body:
        text = text.replace("---\n" + ORPHAN_LINE + "\n# Summary", "---\n# Summary")
    if refresh:
        text = text.replace(
            "  by: 'process:ai-wiki-curator'\n  at: '2026-09-23T15:46:00Z'",
            f"  by: {audit.AUDITOR}\n  at: '{ORPHAN_AUDIT_START}'",
        )
    return text.replace("status: draft", "status: stable")


ORPHAN_EDITS = {
    # 71ea85ca9c20 / e94c8b707aea: lift + new event + refreshed generated.
    "lift_new_refresh": lambda text: _lift_orphan(text),
    # f7dddac6e389: the reviewer ignored the orphan and appended its event.
    "ignore_orphan": lambda text: _lift_orphan(text, refresh=False, keep_body=True).replace(
        HISTORY_TAIL + "\n" + ORPHAN_LINE + "\n", HISTORY_TAIL + "\n", 1,
    ),
    "lift_without_refresh": lambda text: _lift_orphan(text, refresh=False),
    "lift_only": lambda text: _lift_orphan(text, new_event=False, refresh=False),
    "copy_keep_body": lambda text: _lift_orphan(text, keep_body=True),
    "chronological_insert": lambda text: _lift_orphan(text, chronological=True),
    "delete_only": lambda text: text.replace("---\n" + ORPHAN_LINE + "\n", "---\n"),
    # The item lost its `- `: `by`/`at` become top-level frontmatter keys.
    "column0_fields": lambda text: text.replace(
        HISTORY_TAIL + "\n---\n", HISTORY_TAIL + f"\nby: {audit.AUDITOR}\nat: {ORPHAN_AUDIT_START}\n---\n",
    ),
    # e5c00b16c75a again: the reviewer writes its event below the closing delimiter.
    "spill_again": lambda text: text.replace(
        "---\n" + ORPHAN_LINE + "\n", f"---\n  - {{by: {audit.AUDITOR}, at: {ORPHAN_AUDIT_START}}}\n"
        + ORPHAN_LINE + "\n",
    ),
    "untouched": lambda text: text,
}


@pytest.mark.parametrize("edit", sorted(ORPHAN_EDITS))
@pytest.mark.parametrize("verdict", ["verified", "unverified"])
def test_audit_replay_71ea_e94c_orphan_never_fails_and_cleans_body(
    tmp_path: Path, monkeypatch, edit: str, verdict: str,
) -> None:
    """Replay of audits 71ea85ca9c20/e94c8b707aea (parent ingest 6f4b6f97e4b2).

    Both failed with "audit verification timestamp is outside the trusted audit window"
    after lifting the 2026-09-19 orphan into ``verified``. Every reviewer variant now
    finishes, keeps structured history, and removes the spilled line from the body.
    """
    bundle, original = _orphan_bundle(tmp_path)
    concept = bundle / ORPHAN_REL
    job = I.new_audit_job(bundle, "6f4b6f97e4b2", [ORPHAN_REL])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    clock = {"now": ORPHAN_AUDIT_START}
    monkeypatch.setattr(audit.curate, "_now", lambda: clock["now"])

    def reviewer(*args, **kwargs):
        concept.write_text(ORPHAN_EDITS[edit](original), encoding="utf-8")
        clock["now"] = ORPHAN_AUDIT_FINISH
        verdicts = {"verified": [ORPHAN_REL]} if verdict == "verified" else {"unverified": [ORPHAN_REL]}
        return subprocess.CompletedProcess(args[0], 0, stdout=_verdict(**verdicts), stderr="")

    monkeypatch.setattr(curate, "_agent_process", reviewer)
    audit.run(bundle, "6f4b6f97e4b2", path)  # real validate_bundle, no monkeypatch

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "done", result.get("validation")
    assert result["validation"] == {"status": "passed", "error_count": 0}
    assert result["audit"]["status"] == ("passed" if verdict == "verified" else "needs_attention")
    assert result["audit"]["corrected_concepts"] == []
    repairs = result["deterministic_repairs"][ORPHAN_REL]
    assert f"removed spilled frontmatter line from body: {ORPHAN_LINE.strip()!r}" in repairs
    if edit in {"column0_fields", "spill_again"}:  # the reviewer's own slip is visible too
        assert any(repair.startswith(("removed spilled verification field from frontmatter",
                                      "discarded spilled frontmatter line written by editor"))
                   for repair in repairs)
    expected = original.replace("status: draft", "status: stable").replace("---\n" + ORPHAN_LINE + "\n", "---\n")
    if verdict == "verified":
        expected = expected.replace(
            HISTORY_TAIL + "\n", HISTORY_TAIL + f"\n  - {{by: {audit.AUDITOR}, at: {ORPHAN_AUDIT_FINISH}}}\n",
        )
    assert concept.read_text(encoding="utf-8") == expected
    from aiwiki.engine.validate import body_spill_errors
    assert body_spill_errors(audit.parse_doc(concept)[1]) == []


def test_verification_invariant_rejects_rewritten_or_extra_history(tmp_path: Path) -> None:
    concept = tmp_path / "release.md"
    before_fm = yaml.safe_load(_concept()[4:_concept().find("\n---\n", 4)])
    before_fm["verified"] = [{"by": "human:owner", "at": "2026-08-13T00:30:00Z"}]
    before_text = "---\n" + yaml.safe_dump(before_fm, sort_keys=False) + "---\n# Summary\n"
    cases = {
        "history": ([{"by": "human:owner", "at": "2026-08-13T00:40:00Z"}], "must preserve existing"),
        "two_new": (
            [before_fm["verified"][0], {"by": audit.AUDITOR, "at": AUDIT_NOW}, {"by": audit.AUDITOR, "at": AUDIT_NOW}],
            "at most one",
        ),
        "other_actor": ([before_fm["verified"][0], {"by": "human:forged", "at": AUDIT_NOW}], "at most one"),
        "bad_time": ([before_fm["verified"][0], {"by": audit.AUDITOR, "at": "2026-08-13T00:50:00"}], "at most one"),
    }
    for events, expected in cases.values():
        after_fm = dict(before_fm, verified=events)
        concept.write_text("---\n" + yaml.safe_dump(after_fm, sort_keys=False) + "---\n# Summary\n", encoding="utf-8")
        assert any(expected in error for error in audit._verification_policy_errors("a.md", concept, before_text))
    concept.write_text(before_text, encoding="utf-8")
    assert audit._verification_policy_errors("a.md", concept, before_text) == []


def test_audit_accepts_verdict_after_long_agent_run(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    clock = {"now": AUDIT_NOW, "finish": "2026-08-13T01:10:00Z"}
    result = _review(bundle, monkeypatch, lambda path: None, _verdict(verified=["features/release.md"]), clock)
    assert result["status"] == "done"
    assert result["audit"]["status"] == "passed"
    assert "deterministic_repairs" not in result
    verified = _frontmatter(bundle / "features" / "release.md")["verified"]
    assert verified == [{"by": audit.AUDITOR, "at": audit._instant("2026-08-13T01:10:00Z")}]


@pytest.mark.parametrize("spoof", ["future_time", "actor"])
def test_audit_restores_bookkeeping_only_generation_edit(tmp_path: Path, monkeypatch, spoof: str) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    original = concept.read_text(encoding="utf-8")

    def bookkeeping_only(path: Path) -> None:
        def change(frontmatter: dict) -> None:
            if spoof == "actor":
                frontmatter["generated"]["by"] = audit.AUDITOR
            else:
                frontmatter["generated"] = {"by": audit.AUDITOR, "at": "2099-01-01T00:00:00Z"}
        _rewrite(path, change)

    result = _review(bundle, monkeypatch, bookkeeping_only, _verdict(unverified=["features/release.md"]))
    assert result["status"] == "done" and result["audit"]["status"] == "needs_attention"
    assert result["deterministic_repairs"] == {
        "features/release.md": ["restored generated after discarded non-substantive edits"],
    }
    assert concept.read_text(encoding="utf-8") == original.replace("status: draft", "status: stable")


def test_audit_restores_removed_human_verification(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    human = {"by": "human:owner", "at": "2026-08-13T00:30:00Z"}
    _rewrite(concept, lambda frontmatter: frontmatter.update(verified=[human]))
    original = concept.read_text(encoding="utf-8")

    def delete_human_history(path: Path) -> None:
        _rewrite(path, lambda frontmatter: frontmatter.pop("verified"))

    result = _review(bundle, monkeypatch, delete_human_history, _verdict(unverified=["features/release.md"]))
    assert result["status"] == "done"
    assert result["deterministic_repairs"] == {
        "features/release.md": ["restored service-owned verification history"],
    }
    assert concept.read_text(encoding="utf-8") == original.replace("status: draft", "status: stable")


@pytest.mark.parametrize(
    "resource",
    ["https://example.test/untrusted", "/sources/missing.md.source"],
)
def test_audit_restores_source_retarget_before_commit(
    tmp_path: Path, monkeypatch, resource: str,
) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"

    def retargeting_review(path: Path) -> None:
        def change(frontmatter: dict) -> str:
            frontmatter["status"] = "stable"
            frontmatter["generated"] = {"by": audit.AUDITOR, "at": AUDIT_NOW}
            frontmatter["sources"] = [{"id": "retargeted", "resource": resource}]
            return "# Summary\n\nThe feature may have been merged.\n"
        _rewrite(path, change)

    result = _review(bundle, monkeypatch, retargeting_review, _verdict(verified=["features/release.md"]))
    assert result["status"] == "done"
    assert result["audit"]["status"] == "passed"
    assert result["deterministic_repairs"] == {
        "features/release.md": ["restored immutable sources provenance"]
    }
    repaired = _frontmatter(concept)
    assert repaired["sources"] == [
        {"id": "release-source", "resource": "/sources/release.md.source"}
    ]


def test_runtime_failed_attempt_can_be_retried_without_overwriting_history(
    tmp_path: Path, monkeypatch
) -> None:
    bundle = _bundle(tmp_path)
    first = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    first_path = I.job_path(bundle, first["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    monkeypatch.setattr(
        curate,
        "_agent_process",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout="", stderr="temporary reviewer outage"
        ),
    )

    audit.run(bundle, "ingest1", first_path)
    first_result = json.loads(first_path.read_text(encoding="utf-8"))
    assert first_result["status"] == "failed"

    retry, deduplicated = I.receive_audit(bundle, "ingest1", ["features/release.md"])
    assert retry["id"] != first["id"] and deduplicated is False

    def successful_review(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 0, stdout=_verdict(verified=["features/release.md"]), stderr="",
        )

    monkeypatch.setattr(curate, "_agent_process", successful_review)
    retry_path = I.job_path(bundle, retry["id"])
    audit.run(bundle, "ingest1", retry_path)

    retry_result = json.loads(retry_path.read_text(encoding="utf-8"))
    assert retry_result["status"] == "done"
    assert retry_result["audit"]["status"] == "passed"
    assert json.loads(first_path.read_text(encoding="utf-8")) == first_result
