"""Writer failure receipts: one retry taxonomy, structured records, diagnosable timeouts."""
from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from aiwiki import version
from aiwiki.cli import maintain as runner
from aiwiki.engine.document import OKFDocumentError, parse_document
from aiwiki.runtime import audit, curate
from aiwiki.runtime.config import DEFAULT_TIMEOUTS, load_agent_timeouts
from aiwiki.runtime.failure import classify, model_output_error, output_tail, redact
from aiwiki.service import ingest as I
from aiwiki.service import worker

TOKEN = "sk-live-0123456789abcdefghij"
# Live concept risks/ai-writing-pricing-distributed-rate-limit-release-gate-2026-08.md (bundle
# a358395): its path, title and sources name "rate-limit". The last line carries the kind of
# unquoted-colon slip that failed 9de6461bab99.
RATE_LIMIT_PATH = "risks/ai-writing-pricing-distributed-rate-limit-release-gate-2026-08.md"
RATE_LIMIT_SLIP = """---
type: Risk
title: AI Writing 定价接口分布式限流 release gate（AWS WAF 证据缺口，2026-08）
sources:
- id: deployment-solvely-web-ai-writing-pricing-rate-limit-main-2026-08-30
  title: deployment-solvely-web main delta — AI Writing pricing edge rate-limit release contract: WAF
---
# Risk
"""


def _slip() -> OKFDocumentError:
    with pytest.raises(OKFDocumentError) as caught:
        parse_document(RATE_LIMIT_SLIP)
    return caught.value


@pytest.mark.parametrize("job,expected", [
    # Real 0.2.x receipt shapes (errors verbatim from production or the code that writes them).
    ({"error": "interrupted by service restart: interrupted transaction rolled back to base revision",
      "phase": "rolled_back"}, ("interrupted", True, 60)),
    ({"error": "interrupted by service restart: failed to reset interrupted transaction to base revision",
      "phase": "syncing"}, ("internal", False, None)),
    ({"error": "Selected model is at capacity. Please try a different model."}, ("capacity", True, 3600)),
    ({"error": "You've hit your usage limit. Try again at Sep 16th, 2026 7:31 PM."}, ("capacity", True, 3600)),
    ({"error": "adversarial audit failed", "stderr": "unexpected status 429 Too Many Requests"},
     ("capacity", True, 3600)),
    ({"error": "adversarial audit failed", "stderr": "ERROR: stream disconnected before completion"},
     ("transient", True, 300)),
    ({"error": "server rejected the request (status 503): bundle mutation in progress; retry"},
     ("transient", True, 300)),
    ({"error": "curation git commit/push failed", "git": {"note": "rebase conflict; retry from remote"}},
     ("conflict", True, 300)),
    ({"error": "curation git commit/push failed", "git": {"note": "no remote"}}, ("transient", True, 300)),
    ({"error": "curation timed out after 900s"}, ("timeout", True, 300)),
    ({"error": "curation repair timed out after 300s"}, ("timeout", True, 300)),
    ({"error": "curation modified files outside its content scope"}, ("model_output", True, 300)),
    ({"error": "bundle validation failed with 1 error(s)", "validation": {"status": "failed"}},
     ("model_output", True, 300)),
    ({"error": "login required"}, ("auth", False, None)),
    ({"error": "OSError(28, 'No space left on device')"}, ("disk", False, None)),
    ({"error": "parent ingest job not found: abc"}, ("input", False, None)),
    ({"error": "working tree is not clean before curation"}, ("internal", True, 300)),
    ({"error": "RuntimeError('deterministic audit append_log closeout failed')"}, ("internal", True, 300)),
])
def test_legacy_receipts_map_to_contract_classes(job, expected):
    result = classify({"status": "failed", **job})
    assert (result["class"], result["retryable"], result["retry_after_s"]) == expected
    assert set(result) == {"class", "retryable", "retry_after_s", "stage", "detail"}


def test_model_written_rate_limit_text_never_reads_as_capacity():
    # Capacity has no attempt cap and stops the batch, so model text must never select it.
    exc = _slip()
    assert "rate-limit" in repr(exc) and model_output_error(exc)
    job = {"status": "failed", "error": repr(exc), "phase": "rolled_back",
           "validation": {"status": "not_run", "reason": repr(exc)}}
    assert (classify(job)["class"], classify(job)["stage"]) == ("model_output", "validation")
    # An agent transcript that read the file, then lost its stream: transient, not capacity.
    stderr = (f"exec sed -n 1,80p {RATE_LIMIT_PATH}\n- pricing edge rate limit\n"
              "ERROR: stream disconnected before completion")
    job = {"status": "failed", "error": stderr, "validation": {"status": "not_run", "reason": "curation failed"}}
    assert classify(job)["class"] == "transient"
    job = {"status": "failed", "error": "adversarial audit failed", "stderr": stderr.rsplit("\n", 1)[0]}
    assert classify(job)["class"] == "internal"
    assert classify({"status": "failed", "error": f"RuntimeError('refusing to apply non-concept agent "
                                                  f"change: {RATE_LIMIT_PATH}')"})["class"] == "model_output"


def test_malformed_model_output_exception_is_model_output_at_the_catch_site(tmp_path, monkeypatch):
    bundle, job_path = _curation_bundle(tmp_path, monkeypatch)

    def slip(command, **kwargs):
        raise _slip()

    monkeypatch.setattr(curate, "_agent_process", slip)
    curate.run(bundle, "sources/inbox/n.md.source", job_path)
    job = json.loads(job_path.read_text())
    assert job["failure"]["class"] == "model_output" and job["failure"]["stage"] == "validation"

    bundle = _audit_bundle(tmp_path)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    audit.run(bundle, "ingest1", I.job_path(bundle, job["id"]))
    receipt = I.read_job(bundle, job["id"])
    assert receipt["phase"] == "rolled_back" and receipt["error"].startswith("OKFDocumentError(")
    assert receipt["failure"]["class"] == "model_output"


def test_agent_transcript_names_do_not_masquerade_as_auth_failures():
    # Codex stderr quotes the concepts it touched; the provider error is at the tail.
    stderr = "edited experiments/ai-study-deferred-login-ab.md\n" + "x" * 800 + "\nstream error: 502 Bad Gateway"
    job = {"status": "failed", "error": stderr, "validation": {"status": "not_run", "reason": "curation failed"}}
    assert classify(job)["class"] == "transient" and classify(job)["stage"] == "agent"


def test_structured_failure_wins_over_legacy_text():
    job = {"status": "failed", "error": "bundle validation failed", "validation": {"status": "failed"},
           "failure": {"class": "capacity", "retryable": True, "retry_after_s": 120, "stage": "agent",
                       "detail": f"Bearer {TOKEN}"}}
    result = classify(job)
    assert (result["class"], result["retry_after_s"], result["stage"]) == ("capacity", 120, "agent")
    assert TOKEN not in result["detail"]
    assert classify({**job, "failure": {"class": "unknown"}})["class"] == "model_output"


def test_redaction_and_output_tail():
    text = (f"Authorization: Bearer {TOKEN}\napi_key={TOKEN} token: abc123secret "
            "https://user:pa55@example.com/repo.git ghp_" + "a" * 30)
    cleaned = redact(text)
    assert TOKEN not in cleaned and "abc123secret" not in cleaned and "pa55" not in cleaned
    assert "ghp_" not in cleaned and "example.com/repo.git" in cleaned
    for secret, text in (("gw_live_9f8e7d6c5b4a39281706", "ANTHROPIC_AUTH_TOKEN=gw_live_9f8e7d6c5b4a39281706"),
                         ("eb17c0ffee1234567890abcd", "export AIWIKI_TOKEN=eb17c0ffee1234567890abcd"),
                         ("proj-abc123", 'OPENAI_API_KEY="proj-abc123"'),
                         ("wJalrXUtnFEMI", "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI")):
        assert secret not in redact(text), text
    assert redact("max_tokens=4000 input_tokens: 12") == "max_tokens=4000 input_tokens: 12"
    tail = output_tail(b"stdout " + b"y" * 5000, f"stderr Bearer {TOKEN}")
    assert len(tail) == 4000 and tail.endswith("y") and TOKEN not in tail


def _curation_bundle(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle"
    source = bundle / "sources" / "inbox" / "n.md.source"
    source.parent.mkdir(parents=True)
    source.write_text("raw evidence", encoding="utf-8")
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({"source": "sources/inbox/n.md.source", "status": "queued"}))
    monkeypatch.setenv("AIWIKI_GIT", "off")
    return bundle, job_path


def test_curation_timeout_keeps_redacted_partial_output_and_structured_failure(tmp_path, monkeypatch):
    # a658b12f157b timed out with no diagnostics: the partial agent output was discarded.
    bundle, job_path = _curation_bundle(tmp_path, monkeypatch)

    def slow_agent(command, **kwargs):
        assert kwargs["timeout"] == curate.TIMEOUT_S
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"partial answer",
                                        stderr=f"stream retry 3/5 Bearer {TOKEN}".encode())

    monkeypatch.setattr(curate, "_agent_process", slow_agent)
    curate.run(bundle, "sources/inbox/n.md.source", job_path)
    job = json.loads(job_path.read_text())
    assert job["status"] == "failed" and job["phase"] == "rolled_back"
    assert job["error"] == f"curation timed out after {curate.TIMEOUT_S}s"
    assert job["failure"]["class"] == "timeout" and job["failure"]["stage"] == "agent"
    assert "stream retry 3/5" in job["agent"]["output_tail"] and "partial answer" in job["agent"]["output_tail"]
    assert TOKEN not in json.dumps(job)
    assert job["service"] == version.service_identity()


def test_curation_git_timeout_is_transient_not_a_curation_timeout(tmp_path, monkeypatch):
    bundle, job_path = _curation_bundle(tmp_path, monkeypatch)

    def agent_then_git_timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(["git", "-C", str(bundle), "push"], curate.GIT_TIMEOUT_S)

    monkeypatch.setattr(curate, "_agent_process", agent_then_git_timeout)
    curate.run(bundle, "sources/inbox/n.md.source", job_path)
    job = json.loads(job_path.read_text())
    assert job["error"].startswith("curation git timed out")
    assert job["failure"]["class"] == "transient" and job["failure"]["stage"] == "git"


def test_curation_nonzero_exit_records_output_tail_and_capacity(tmp_path, monkeypatch):
    bundle, job_path = _curation_bundle(tmp_path, monkeypatch)
    monkeypatch.setattr(curate, "_agent_process", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 1, stdout="", stderr=f"key={TOKEN}\nERROR: Selected model is at capacity."))
    curate.run(bundle, "sources/inbox/n.md.source", job_path)
    job = json.loads(job_path.read_text())
    assert job["failure"]["class"] == "capacity" and job["failure"]["stage"] == "agent"
    assert "at capacity" in job["agent"]["output_tail"] and TOKEN not in json.dumps(job)


def _audit_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "kb"
    (bundle / "features").mkdir(parents=True)
    (bundle / "sources").mkdir()
    (bundle / ".okf" / "jobs").mkdir(parents=True)
    frontmatter = {"type": "Feature", "title": "Claim", "description": "d", "tags": ["x"], "status": "draft",
                   "generated": {"by": "process:ai-wiki-curator", "at": "2026-08-13T00:00:00Z"},
                   "sources": [{"id": "s", "resource": "/sources/release.md.source"}]}
    (bundle / "features" / "release.md").write_text(
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n# Claim\n", encoding="utf-8")
    source = bundle / "sources" / "release.md.source"
    source.write_text("evidence\n", encoding="utf-8")
    I.save_job(bundle, {"id": "ingest1", "kind": "ingest", "status": "done", "source": "sources/inbox/r.md.source",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "validation": {"status": "passed"}, "concept_files": ["features/release.md"]})
    return bundle


def test_audit_agent_429_is_capacity_and_stops_the_maintained_batch(tmp_path, monkeypatch):
    # Before: audit put the provider error only in job.stderr, so maintain saw
    # "adversarial audit failed" and demanded manual repair (fix-history F1).
    bundle = _audit_bundle(tmp_path)
    job = I.new_audit_job(bundle, "ingest1", ["features/release.md"])
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(curate, "_agent_process", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 1, stdout="", stderr="HTTP 429 Too Many Requests: usage limit reached"))
    audit.run(bundle, "ingest1", I.job_path(bundle, job["id"]))
    receipt = I.read_job(bundle, job["id"])
    assert receipt["failure"]["class"] == "capacity" and receipt["phase"] == "rolled_back"
    assert "429" in receipt["agent"]["output_tail"] and receipt["service"]["version"] == version.VERSION

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state = {"version": 1, "endpoint": "https://wiki", "bundle": "kb", "sources": [
        {"identity": "a", "sha256": "s", "status": "pending", "audit": [receipt],
         "ingest": [{"id": "ingest1", "kind": "ingest", "status": "done", "sha256": "s",
                     "validation": {"status": "passed"}, "changed_files": [], "git": {"note": "no changes"}}]},
        {"identity": "b", "sha256": "t", "status": "pending", "ingest": [], "audit": [], "path": "/unused"},
    ]}
    monkeypatch.setattr(runner, "cli", lambda *args, **kwargs: pytest.fail(f"no request expected: {args}"))
    result = runner.run_sources(state, state_dir / "state.json", "kb", poll=0)
    assert result["writer_retry"]["kind"] == "capacity" and result["pending"] == 2


def test_restart_interruption_records_retryable_interrupted_failure(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for key, value in (("user.name", "T"), ("user.email", "t@example.com")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    (repo / ".okf" / "jobs").mkdir(parents=True)
    (repo / ".gitignore").write_text(".okf/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    job_path = repo / ".okf" / "jobs" / "j.json"
    job_path.write_text(json.dumps({"id": "j", "kind": "ingest", "status": "running", "phase": "curating",
                                    "base_revision": base, "base_branch": "main"}))
    assert worker.recover([repo]) is True
    job = json.loads(job_path.read_text())
    assert job["phase"] == "rolled_back"
    assert job["failure"] == {"class": "interrupted", "retryable": True, "retry_after_s": 60,
                              "stage": "startup", "detail": job["error"]}


def test_worker_classifies_failed_jobs_that_returned_early(tmp_path, monkeypatch):
    job_path = tmp_path / "early.json"

    def dirty_tree(_bundle, _source, path):
        path.write_text(json.dumps({"id": "early", "status": "failed",
                                    "error": "working tree is not clean before curation"}))

    monkeypatch.setattr(worker.curate, "run", dirty_tree)
    worker.ensure_started()
    worker.submit(tmp_path, "sources/inbox/x", job_path)
    worker._q.join()
    assert json.loads(job_path.read_text())["failure"]["class"] == "internal"


def test_jobs_are_stamped_with_service_identity_and_health_reports_build(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_BUILD", [])
    monkeypatch.setenv("AIWIKI_BUILD_COMMIT", "abc1234")
    assert version.service_identity() == {"version": version.VERSION, "build": "abc1234"}
    bundle = _audit_bundle(tmp_path)
    ingest = I.new_job(bundle, "sources/inbox/r.md.source", "f" * 64, True)
    assert ingest["service"] == {"version": version.VERSION, "build": "abc1234"}
    assert I.new_audit_job(bundle, "ingest1", ["features/release.md"])["service"]["build"] == "abc1234"

    monkeypatch.setenv("AIWIKI_BUNDLE", str(bundle))
    monkeypatch.delenv("AIWIKI_BUNDLES", raising=False)
    monkeypatch.setenv("AIWIKI_TOKEN", "testtok")
    monkeypatch.setenv("AIWIKI_CURATE", "off")
    from aiwiki.service import app as appmod
    importlib.reload(appmod)
    from fastapi.testclient import TestClient
    health = TestClient(appmod.app).get("/health", headers={"Authorization": "Bearer testtok"}).json()
    assert health["build"] == "abc1234" and health["service_version"] == version.VERSION


def test_build_falls_back_to_deployed_revision_marker(tmp_path, monkeypatch):
    # <app root>/src/aiwiki/version.py -> <app root>/.ai-wiki-deployed-revision
    monkeypatch.setattr(version, "__file__", str(tmp_path / "src" / "aiwiki" / "version.py"))
    monkeypatch.delenv("AIWIKI_BUILD_COMMIT", raising=False)
    monkeypatch.setattr(version, "_BUILD", [])
    assert version.build() is None
    (tmp_path / ".ai-wiki-deployed-revision").write_text("68a5e53\nextra\n", encoding="utf-8")
    monkeypatch.setattr(version, "_BUILD", [])
    assert version.build() == "68a5e53"


@pytest.fixture
def timeout_config(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "config.json"
    monkeypatch.setenv("AIWIKI_CONFIG", str(path))
    for key in DEFAULT_TIMEOUTS:
        monkeypatch.delenv(f"AIWIKI_AGENT_{key.upper()}", raising=False)
    return path


def test_agent_timeouts_default_config_and_env(timeout_config: Path, monkeypatch):
    timeout_config.write_text("{}")
    assert load_agent_timeouts() == {"timeout_s": 1500, "audit_timeout_s": 1200, "repair_timeout_s": 600}
    timeout_config.write_text(json.dumps({"agent": {"model": "m", "timeout_s": 1800, "repair_timeout_s": 900}}))
    assert load_agent_timeouts() == {"timeout_s": 1800, "audit_timeout_s": 1200, "repair_timeout_s": 900}
    monkeypatch.setenv("AIWIKI_AGENT_AUDIT_TIMEOUT_S", "2400")
    monkeypatch.setenv("AIWIKI_AGENT_TIMEOUT_S", "60")
    assert load_agent_timeouts() == {"timeout_s": 60, "audit_timeout_s": 2400, "repair_timeout_s": 900}


@pytest.mark.parametrize("agent,env", [
    ({"timeout_s": 59}, None), ({"timeout_s": 7201}, None), ({"audit_timeout_s": "900"}, None),
    ({"repair_timeout_s": True}, None), ({"timeout_s": 900.5}, None), ({}, "12m"), ({}, "-5"), ({}, ""),
])
def test_invalid_agent_timeouts_fail_closed(timeout_config: Path, monkeypatch, agent, env):
    timeout_config.write_text(json.dumps({"agent": agent}))
    if env is not None:
        monkeypatch.setenv("AIWIKI_AGENT_TIMEOUT_S", env)
    with pytest.raises(ValueError, match="integer number of seconds"):
        load_agent_timeouts()
