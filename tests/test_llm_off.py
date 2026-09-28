"""AIWIKI_LLM=off (the final state): the writer is the deterministic gate only and never starts
an agent process on any path, including the legacy Codex ones, and it never reads config.agent.

Process creation is blocked at ``subprocess.Popen``, which ``subprocess.run`` goes through too,
so a Codex pass anywhere (curate, audit, restart recovery, the sweeper, a queued job) fails the
test. The real curate and audit runtimes stay in place.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from gate_fixture import EVIDENCE, METRIC, Gate, git

from aiwiki.cli import maint
from aiwiki.runtime import audit, curate
from aiwiki.service import ingest as I
from aiwiki.service import worker


def _refuse(attempts: list):
    def popen(args, *_args, **_kwargs):
        attempts.append(args)
        raise AssertionError(f"a process was started under AIWIKI_LLM=off: {args!r}")
    return popen


def _gate(tmp_path: Path, monkeypatch, **env: str) -> Gate:
    real = audit.run  # the fixture swaps in a recorder; these tests keep the real reviewer
    gate = Gate(tmp_path, monkeypatch, AIWIKI_LLM="off", **env)
    monkeypatch.setattr(worker.audit, "run", real)
    return gate


def test_no_legacy_path_starts_a_process(tmp_path, monkeypatch) -> None:
    gate = _gate(tmp_path, monkeypatch)
    owner = gate.headers("owner")
    health = gate.client.get("/health", params={"bundle": "kb-a"}, headers=owner).json()
    assert health["writer_agent"] == {"runtime": "off"}
    # Legacy work a Codex writer left behind: a queued ingest, a queued audit, an inbox drop.
    inbox = gate.writer / "sources" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "left.md").write_bytes(EVIDENCE)
    ingest_job = I.new_job(gate.writer, "sources/inbox/left.md", "0" * 64, True, filename="left.md")
    audit_job = I.new_audit_job(gate.writer, ingest_job["id"], [METRIC])
    (inbox / "dropped.md").write_bytes(b"# dropped out of band\n")
    before = set(gate.jobs())
    swept, attempts = [], []
    # The sweeper's timer thread would outlive the test: sweep once, at startup, instead.
    monkeypatch.setattr(worker, "start_sweeper", lambda bundles: swept.append(worker.sweep_once(bundles())))
    monkeypatch.setattr(subprocess, "Popen", _refuse(attempts))

    with TestClient(gate.appmod.app) as client:  # startup: recovery, the worker, the sweeper
        assert client.get("/whoami", headers=owner).json()["modes"]["llm"] == "off"
        submitted = client.post("/ingest", params={"bundle": "kb-a"}, headers=owner,
                                json={"text": "# a member's notes\n", "title": "notes"})
        assert submitted.status_code == 409 and "AIWIKI_LLM=off" in submitted.json()["detail"]
        for token in ("owner", "curator", "auditor"):
            requested = client.post(f"/jobs/{ingest_job['id']}/audit", params={"bundle": "kb-a"},
                                    headers=gate.headers(token))
            assert requested.status_code == 409 and "AIWIKI_LLM=off" in requested.json()["detail"], token

    assert swept == [0] and worker.sweep_once([gate.writer]) == 0
    for submit in (lambda: worker.submit(gate.writer, "sources/inbox/left.md", I.job_path(gate.writer, "x")),
                   lambda: worker.submit_audit(gate.writer, ingest_job["id"], I.job_path(gate.writer, "y"))):
        with pytest.raises(curate.AgentDisabled):
            submit()
    with pytest.raises(curate.AgentDisabled):  # the last line: even a direct call starts nothing
        curate._run_agent(curate._codex_command(gate.writer, "curate"), cwd=gate.writer, timeout=5)
    worker._q.join()
    assert attempts == []
    assert set(gate.jobs()) == before  # nothing new was registered or stored
    assert sorted(path.name for path in inbox.iterdir()) == ["dropped.md", "left.md"]
    # What the Codex writer left stays queued for a rollback to AIWIKI_LLM=codex.
    assert gate.job(ingest_job["id"])["status"] == gate.job(audit_job["id"])["status"] == "queued"


def test_a_changeset_runs_only_git_and_queues_no_codex_audit(tmp_path, monkeypatch) -> None:
    gate = _gate(tmp_path, monkeypatch)
    real, started = subprocess.Popen, []

    def only_git(args, *rest, **kwargs):
        started.append(Path(args[0]).name)
        if Path(args[0]).name != "git":
            raise AssertionError(f"a non-Git process was started under AIWIKI_LLM=off: {args!r}")
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", only_git)
    item_id = gate.item(run="WAIO-1")

    response = gate.post(gate.item_request(item_id), run="WAIO-1")

    job = response.json()
    assert response.status_code == 201, response.text
    assert job["commit"] == gate.remote_head() and job["audit"] == {"mode": "llm_off"}
    assert [stem for stem in gate.jobs() if gate.job(stem)["kind"] == "audit"] == []
    # Unaudited, it waits where a rollback's `maint begin` would resubmit it.
    assert [row["id"] for row in I.pending_audits(gate.writer, older_than_hours=0)["jobs"]] == [job["id"]]
    assert set(started) == {"git"}
    git(gate.writer, "status", "--porcelain")  # the gate left a clean tree (and git still runs)


def test_maint_begin_resubmits_no_codex_audit_to_a_writer_without_one(monkeypatch) -> None:
    calls = []

    def call(method, route, **_kwargs):
        calls.append((method, route))
        return 200, {"modes": {"audit": "codex", "llm": "off"}, "service": {"build": "b"}}

    monkeypatch.setattr(maint, "_call", call)
    assert maint._audits("kb-a") == {"mode": "llm_off", "resubmitted": [], "needs_human": []}
    assert calls == [("GET", "/whoami")]


def test_an_unknown_llm_mode_refuses_to_start(tmp_path, monkeypatch) -> None:
    gate = Gate(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="AIWIKI_LLM must be one of codex, off; got 'claude'"):
        gate.app(AIWIKI_LLM="claude")
    gate.app()  # leave a loadable service for the fixture's teardown


def test_the_agent_config_is_never_read(tmp_path: Path) -> None:
    """A fresh interpreter, as the writer starts: an agent config that would fail startup and
    names a working agent binary is ignored under AIWIKI_LLM=off, and nothing runs it."""
    marker = tmp_path / "agent-ran"
    binary = tmp_path / "fake codex"
    binary.write_text(f"#!{sys.executable}\nopen({str(marker)!r}, 'w').write('ran')\n")
    binary.chmod(0o700)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"agent": {"bin": str(binary), "model": 42, "timeout_s": 5}}))
    script = """
import json
from pathlib import Path
from aiwiki.runtime import audit, curate
try:
    curate._agent_process([curate.AGENT_BIN or 'codex', 'exec'], cwd=Path.cwd(), timeout=5)
    outcome = 'ran'
except curate.AgentDisabled as exc:
    outcome = str(exc)
print(json.dumps({'agent': curate._agent_metadata(), 'enabled': curate.agents_enabled(),
                  'timeouts': [curate.TIMEOUT_S, audit.TIMEOUT_S], 'outcome': outcome}))
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "AIWIKI_CONFIG": str(config),
           "AIWIKI_AGENT_BIN": str(binary)}

    off = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env={**env, "AIWIKI_LLM": "off"},
                         text=True, capture_output=True, timeout=30)
    codex = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env,
                           text=True, capture_output=True, timeout=30)

    assert off.returncode == 0, off.stderr
    assert json.loads(off.stdout) == {"agent": {"runtime": "off"}, "enabled": False, "timeouts": [1500, 1200],
                                      "outcome": "AIWIKI_LLM=off: this server never starts an agent process"}
    assert not marker.exists()
    # The same config under the default is read, and refused.
    assert codex.returncode != 0 and "config.agent.timeout_s must be an integer" in codex.stderr
