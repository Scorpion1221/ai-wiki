"""Adversarial audit API/runtime: scope, trust promotion, idempotency and failures."""
from __future__ import annotations

import importlib
import json
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

from aiwiki.runtime import audit
from aiwiki.service import ingest as I


def test_audit_prompt_matches_generation_and_verification_policy() -> None:
    assert "change only `status` and `verified`" in audit.AUDIT_PROMPT
    assert "change ANY frontmatter or body content" in audit.AUDIT_PROMPT
    assert "refresh `generated`" in audit.AUDIT_PROMPT

AUTH = {"Authorization": "Bearer testtok"}
AUDIT_NOW = "2026-08-13T01:00:00Z"


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
    monkeypatch.setattr(audit.subprocess, "run", unexpected)

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


def test_audit_endpoint_fails_closed_during_bundle_mutation(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    client, submitted = _client(bundle, monkeypatch)
    from aiwiki.service import app as appmod

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
    finally:
        release.set()
        thread.join(timeout=5)

    assert response.status_code == 503
    assert submitted == []
    jobs = []
    for path in (bundle / ".okf" / "jobs").glob("*.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("kind") == "audit":
            jobs.append(job)
    assert jobs == []


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

    def fake_claude(*args, **kwargs):
        assert AUDIT_NOW in args[0][2]
        if verify:
            (bundle / "features" / "release.md").write_text(_concept(verified=True), encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, stdout="reviewed", stderr="")

    monkeypatch.setattr(audit.subprocess, "run", fake_claude)
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


def test_audit_closeout_runs_after_agent_scope_gate_and_logs_scoped_concepts(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)
    events = []

    def fake_claude(*args, **kwargs):
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

    monkeypatch.setattr(audit.subprocess, "run", fake_claude)
    monkeypatch.setattr(audit, "_deterministic_closeout", fake_closeout)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "done"
    assert events == ["agent", "closeout"]
    assert set(result["changed_files"]) == {"features/release.md", "index.md", "log.md"}


def test_audit_uses_same_locked_content_tool_boundary(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    captured = []

    def fake_claude(command, **kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="reviewed", stderr="")

    monkeypatch.setattr(audit.subprocess, "run", fake_claude)
    audit.run(bundle, "ingest1", path)
    command = captured[0]
    assert command[command.index("--tools") + 1] == "Read,Edit,Write"
    scope = "//" + bundle.resolve().as_posix().lstrip("/") + "/**"
    assert command[command.index("--allowedTools") + 1] == (
        f"Read({scope}),Edit({scope})"
    )
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert "bypassPermissions" not in command
    assert not any(tool in command for tool in ("Bash", "Glob", "Grep", "WebFetch", "Skill", "Agent"))


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
        if command[0] != "claude":
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
    monkeypatch.setattr(audit.subprocess, "run", concurrent_enqueue)
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
    real_run = subprocess.run
    attacked = False
    safe_git_calls = 0

    def metadata_attack(command, *args, **kwargs):
        nonlocal attacked, safe_git_calls
        if command[0] == "claude":
            config.write_text("[core]\n\thooksPath = .git/hooks\n", encoding="utf-8")
            hook.write_text(f"#!/bin/sh\ntouch {executed}\n", encoding="utf-8")
            hook.chmod(0o755)
            attacked = True
            raise subprocess.TimeoutExpired(command, audit.TIMEOUT_S)
        if attacked and command[0] == "git":
            safe_git_calls += 1
            assert config.read_bytes() == original_config
            assert not hook.exists()
        return real_run(command, *args, **kwargs)

    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(audit.subprocess, "run", metadata_attack)
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
        audit.subprocess,
        "run",
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

    monkeypatch.setattr(audit.subprocess, "run", bad_review)
    audit.run(bundle, "ingest1", path)
    job = json.loads(path.read_text(encoding="utf-8"))
    assert job["status"] == "failed" and job["audit"]["status"] == "failed"
    assert job["validation"]["status"] == "failed"
    assert job["validation"]["errors"] == ["features/release.md: bad"]
    assert (bundle / "features" / "release.md").read_text(encoding="utf-8") == original


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

    monkeypatch.setattr(audit.subprocess, "run", failed_agent)
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

    monkeypatch.setattr(audit.subprocess, "run", malicious_agent)
    audit.run(bundle, "ingest1", path)
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["out_of_scope_files"] == ["purpose.md"]
    assert purpose.read_text(encoding="utf-8") == "original"


def test_audit_rejects_substantive_correction_without_generated_refresh(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    original = concept.read_text(encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")

    def stale_generation(*args, **kwargs):
        concept.write_text(original.replace("The feature was merged.", "The feature may be merged."), encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, stdout="corrected", stderr="")

    monkeypatch.setattr(audit.subprocess, "run", stale_generation)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    audit.run(bundle, "ingest1", path)
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert any("must set generated.by" in error for error in result["validation"]["errors"])
    assert concept.read_text(encoding="utf-8") == original


def test_audit_rejects_forged_human_verification(tmp_path: Path, monkeypatch) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    original = concept.read_text(encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")

    def forged_review(*args, **kwargs):
        forged = yaml.safe_load(original[4:original.find("\n---\n", 4)])
        forged["status"] = "stable"
        forged["verified"] = [{"by": "human:owner", "at": "2026-08-13T01:00:00Z"}]
        concept.write_text(
            "---\n" + yaml.safe_dump(forged, sort_keys=False) + "---\n# Summary\n\nThe feature was merged.\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args[0], 0, stdout="forged", stderr="")

    monkeypatch.setattr(audit.subprocess, "run", forged_review)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    audit.run(bundle, "ingest1", path)
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert any("unauthorized verifier 'human:owner'" in error for error in result["validation"]["errors"])
    assert any("stable audit result requires" in error for error in result["validation"]["errors"])
    assert concept.read_text(encoding="utf-8") == original


def test_audit_rejects_future_generation_and_verification_and_rolls_back(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    original = concept.read_text(encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)

    def future_review(*args, **kwargs):
        assert AUDIT_NOW in args[0][2]
        forged = yaml.safe_load(original[4:original.find("\n---\n", 4)])
        forged["description"] = "A materially corrected claim"
        forged["status"] = "stable"
        forged["generated"] = {"by": audit.AUDITOR, "at": "2099-01-01T00:00:00Z"}
        forged["verified"] = [{"by": audit.AUDITOR, "at": "2099-01-01T00:00:00Z"}]
        concept.write_text(
            "---\n" + yaml.safe_dump(forged, sort_keys=False)
            + "---\n# Summary\n\nThe feature may have been merged.\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args[0], 0, stdout="forged", stderr="")

    monkeypatch.setattr(audit.subprocess, "run", future_review)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert any("generated.at must not be in the future" in error for error in result["validation"]["errors"])
    assert any("verification timestamp must not be in the future" in error for error in result["validation"]["errors"])
    assert concept.read_text(encoding="utf-8") == original


def test_audit_rejects_bookkeeping_only_future_generation_and_rolls_back(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    original = concept.read_text(encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)

    def bookkeeping_only(*args, **kwargs):
        forged = yaml.safe_load(original[4:original.find("\n---\n", 4)])
        forged["generated"] = {"by": audit.AUDITOR, "at": "2099-01-01T00:00:00Z"}
        concept.write_text(
            "---\n" + yaml.safe_dump(forged, sort_keys=False)
            + "---\n# Summary\n\nThe feature was merged.\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args[0], 0, stdout="forged", stderr="")

    monkeypatch.setattr(audit.subprocess, "run", bookkeeping_only)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert any(
        "must not change generated without a substantive correction" in error
        for error in result["validation"]["errors"]
    )
    assert concept.read_text(encoding="utf-8") == original


def test_audit_rejects_bookkeeping_only_generated_actor_spoof(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    original = concept.read_text(encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)

    def actor_spoof(*args, **kwargs):
        forged = yaml.safe_load(original[4:original.find("\n---\n", 4)])
        forged["generated"]["by"] = audit.AUDITOR
        concept.write_text(
            "---\n" + yaml.safe_dump(forged, sort_keys=False)
            + "---\n# Summary\n\nThe feature was merged.\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args[0], 0, stdout="forged", stderr="")

    monkeypatch.setattr(audit.subprocess, "run", actor_spoof)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert any(
        "must not change generated without a substantive correction" in error
        for error in result["validation"]["errors"]
    )
    assert concept.read_text(encoding="utf-8") == original


def test_audit_rejects_removing_existing_human_verification(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    original_fm = yaml.safe_load(_concept()[4:_concept().find("\n---\n", 4)])
    original_fm["verified"] = [{"by": "human:owner", "at": "2026-08-13T00:30:00Z"}]
    original = (
        "---\n" + yaml.safe_dump(original_fm, sort_keys=False)
        + "---\n# Summary\n\nThe feature was merged.\n"
    )
    concept.write_text(original, encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)

    def delete_human_history(*args, **kwargs):
        forged = dict(original_fm)
        forged.pop("verified")
        concept.write_text(
            "---\n" + yaml.safe_dump(forged, sort_keys=False)
            + "---\n# Summary\n\nThe feature was merged.\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args[0], 0, stdout="deleted", stderr="")

    monkeypatch.setattr(audit.subprocess, "run", delete_human_history)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert any(
        "must preserve existing verification 'human:owner'" in error
        for error in result["validation"]["errors"]
    )
    assert concept.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "resource",
    ["https://example.test/untrusted", "/sources/missing.md.source"],
)
def test_audit_rejects_source_retarget_and_rolls_back(
    tmp_path: Path, monkeypatch, resource: str,
) -> None:
    bundle = _bundle(tmp_path)
    concept = bundle / "features" / "release.md"
    original = concept.read_text(encoding="utf-8")
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    path = I.job_path(bundle, job["id"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(audit.curate, "_now", lambda: AUDIT_NOW)

    def retargeting_review(*args, **kwargs):
        forged = yaml.safe_load(original[4:original.find("\n---\n", 4)])
        forged["status"] = "stable"
        forged["generated"] = {"by": audit.AUDITOR, "at": AUDIT_NOW}
        forged["verified"] = [{"by": audit.AUDITOR, "at": AUDIT_NOW}]
        forged["sources"] = [{"id": "retargeted", "resource": resource}]
        concept.write_text(
            "---\n" + yaml.safe_dump(forged, sort_keys=False)
            + "---\n# Summary\n\nThe feature was merged.\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args[0], 0, stdout="retargeted", stderr="")

    monkeypatch.setattr(audit.subprocess, "run", retargeting_review)
    monkeypatch.setattr(audit, "validate_bundle", lambda _bundle: [])
    audit.run(bundle, "ingest1", path)

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    errors = result["validation"]["errors"]
    assert any("audit must not change sources provenance" in error for error in errors)
    assert any("must retain parent source citation" in error for error in errors)
    if resource.startswith("/"):
        assert any("does not resolve to a local file" in error for error in errors)
    assert concept.read_text(encoding="utf-8") == original


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
        audit.subprocess,
        "run",
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
        (bundle / "features" / "release.md").write_text(_concept(verified=True), encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, stdout="reviewed", stderr="")

    monkeypatch.setattr(audit.subprocess, "run", successful_review)
    retry_path = I.job_path(bundle, retry["id"])
    audit.run(bundle, "ingest1", retry_path)

    retry_result = json.loads(retry_path.read_text(encoding="utf-8"))
    assert retry_result["status"] == "done"
    assert retry_result["audit"]["status"] == "passed"
    assert json.loads(first_path.read_text(encoding="utf-8")) == first_result
