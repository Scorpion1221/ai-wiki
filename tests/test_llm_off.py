"""AIWIKI_LLM=off (the final state): the writer is the deterministic gate only and never starts
an agent process on any path, including the legacy Codex ones, and it never reads config.agent.

Process creation is blocked at ``subprocess.Popen``, which ``subprocess.run`` goes through too,
so a Codex pass anywhere (curate, audit, restart recovery, the sweeper, a queued job) fails the
test. The real curate and audit runtimes stay in place.
"""
from __future__ import annotations

import hashlib
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
from aiwiki.service import maint_state as M
from aiwiki.service import worker


def _refuse(attempts: list):
    def popen(args, *_args, **_kwargs):
        attempts.append(args)
        raise AssertionError(f"a process was started under AIWIKI_LLM=off: {args!r}")
    return popen


EPOCH = "2026-09-27T00:00:00Z"


def _gate(tmp_path: Path, monkeypatch, **env: str) -> Gate:
    """The writer with AIWIKI_LLM=off, and so with the external audit it needs to start."""
    real = audit.run  # the fixture swaps in a recorder; these tests keep the real reviewer
    gate = Gate(tmp_path, monkeypatch, AIWIKI_LLM="off", AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH=EPOCH, **env)
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
            assert requested.status_code == 409 and "GET /audit/backlog" in requested.json()["detail"], token

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
    # The Codex ingest left behind stays queued for a rollback to AIWIKI_LLM=codex; its audit
    # is cancelled, never run, as every Codex audit is under the external audit.
    assert (gate.job(ingest_job["id"])["status"], gate.job(audit_job["id"])["status"]) == ("queued", "cancelled")


def test_the_final_state_takes_members_and_reviews_without_an_agent(tmp_path, monkeypatch) -> None:
    """The cut-over's flags together (runbook step 6): inbox intake and external audit keep
    working under AIWIKI_LLM=off, and none of their paths reaches Codex. Only Git runs."""
    gate = _gate(tmp_path, monkeypatch, AIWIKI_INTAKE="inbox")
    owner = gate.headers("owner")
    inbox = gate.writer / "sources" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "left.md").write_bytes(EVIDENCE)  # a Codex job holds it: the sweep leaves it alone
    ingest_job = I.new_job(gate.writer, "sources/inbox/left.md", hashlib.sha256(EVIDENCE).hexdigest(), True,
                           filename="left.md")
    audit_job = I.new_audit_job(gate.writer, ingest_job["id"], [METRIC])
    (inbox / "dropped.md").write_bytes(b"# dropped out of band\n")
    real, others, swept = subprocess.Popen, [], []

    def only_git(args, *rest, **kwargs):
        if Path(args[0]).name != "git":
            others.append(args)
            raise AssertionError(f"a non-Git process was started under AIWIKI_LLM=off: {args!r}")
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(worker, "start_sweeper", lambda bundles: swept.append(worker.sweep_once(bundles())))
    monkeypatch.setattr(subprocess, "Popen", only_git)

    with TestClient(gate.appmod.app) as client:  # startup: recovery, the worker, the sweeper
        modes = client.get("/whoami", headers=owner).json()["modes"]
        assert (modes["intake"], modes["audit"], modes["llm"]) == ("inbox", "external", "off")
        member = client.post("/ingest", params={"bundle": "kb-a"}, headers=gate.headers("member"),
                             json={"text": "# a member's notes\n", "title": "notes"})
        assert member.status_code == 200 and member.json()["status"] == "ready", member.text
        # kb-b does not commit, so inbox intake does not apply there and only Codex could curate it.
        elsewhere = client.post("/ingest", params={"bundle": "kb-b"}, headers=gate.headers("member"),
                                json={"text": "# a member's notes\n"})
        assert elsewhere.status_code == 409 and "AIWIKI_LLM=off" in elsewhere.json()["detail"]
        requeue = client.post("/admin/inbox/requeue", params={"bundle": "kb-a"}, headers=owner, json={})
        assert requeue.status_code == 409 and "AIWIKI_LLM=off" in requeue.json()["detail"]
        audit_route = client.post(f"/jobs/{ingest_job['id']}/audit", params={"bundle": "kb-a"}, headers=owner)
        assert audit_route.status_code == 409 and "GET /audit/backlog" in audit_route.json()["detail"]
        backlog = client.get("/audit/backlog", params={"bundle": "kb-a"}, headers=gate.headers("auditor"))
        assert backlog.status_code == 200 and backlog.json()["mode"] == "external", backlog.text

    worker._q.join()
    assert others == [] and swept == [1]
    origins = sorted(item["origin"]["via"] for item in M.list_items(gate.writer)["items"])
    assert origins == ["drop", "ingest"]
    # A Codex audit left queued never runs under external audit; a Codex ingest waits for a rollback.
    assert gate.job(audit_job["id"])["status"] == "cancelled"
    assert gate.job(ingest_job["id"])["status"] == "queued"


def test_the_sweeper_still_scans_drops_but_queues_no_codex_curation(tmp_path, monkeypatch) -> None:
    """Only the sweep's Codex branch is off: it still reads each drop, so an intake that registers
    drops without an agent (AIWIKI_INTAKE=inbox, ahead of that branch) keeps working."""
    gate = _gate(tmp_path, monkeypatch)
    inbox = gate.writer / "sources" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "dropped.md").write_bytes(b"# dropped out of band\n")
    real, scanned, attempts = I.is_curatable, [], []
    monkeypatch.setattr(I, "is_curatable", lambda rel, data: scanned.append(rel) or real(rel, data))
    monkeypatch.setattr(subprocess, "Popen", _refuse(attempts))
    before = set(gate.jobs())

    assert worker.sweep_once([gate.writer]) == 0

    assert scanned == ["sources/inbox/dropped.md"] and attempts == []
    assert set(gate.jobs()) == before  # no job, so nothing sits queued for an agent that never comes


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
    assert job["commit"] == gate.remote_head() and job["audit"] == {"mode": "external"}
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


def test_off_refuses_to_start_without_the_external_audit(tmp_path, monkeypatch) -> None:
    """No Codex audit runs under AIWIKI_LLM=off, so with AIWIKI_AUDIT=codex (a partial rollback of
    the cut-over's drop-in) nothing would ever verify, and no alert would say so."""
    gate = Gate(tmp_path, monkeypatch)
    for env in ({}, {"AIWIKI_AUDIT": "codex", "AIWIKI_BACKLOG_EPOCH": EPOCH}):
        with pytest.raises(RuntimeError, match="AIWIKI_LLM=off runs no Codex audit: it needs AIWIKI_AUDIT=external"):
            gate.app(AIWIKI_LLM="off", **env)
    modes = gate.app(AIWIKI_LLM="off", AIWIKI_AUDIT="external", AIWIKI_BACKLOG_EPOCH=EPOCH).MODES
    assert (modes["llm"], modes["audit"]) == ("off", "external")
    gate.app()


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
