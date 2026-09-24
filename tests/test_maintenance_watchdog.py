"""Maintenance watchdog: replays recorded production data (Multica runs/issues/timelines and
writer job receipts) and checks alerting, exit codes and Feishu deduplication."""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "maintenance_watchdog.py"
FIXTURES = ROOT / "tests" / "fixtures" / "maintenance_watchdog"
SNAPSHOT = FIXTURES / "multica_snapshot_20260923.json"
READ_ONLY = {("autopilot", "runs"), ("autopilot", "get"), ("issue", "list"), ("issue", "get"), ("issue", "timeline")}
REPLAY = ["--runs-limit", "100", "--checkpoint-key", "ai_wiki_incremental_checkpoint_v4",
          "--checkpoint-key", "ai_wiki_incremental_checkpoint_v3"]


def run(*args: str, env: dict | None = None) -> tuple[int, dict]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("AIWIKI_WATCHDOG_")}
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env={**base, **(env or {})}, timeout=120)
    assert proc.stdout, proc.stderr
    return proc.returncode, json.loads(proc.stdout)


def multica_bin(snapshot: Path = SNAPSHOT) -> str:
    return shlex.join([sys.executable, str(FIXTURES / "fake_multica.py"), str(snapshot)])


def keys(result: dict) -> set[str]:
    return {a["key"] for a in result["alerts"]}


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Multica: historical replay of production data --------------------------------------------

RUN_146 = "latest_run_failed:0972f9a4-2792-401e-9b1a-feb5febf5a1e"
RUN_499 = "latest_run_failed:01a0a6a7-d34d-7548-aff4-ce9d56875037"
RUN_559 = "latest_run_failed:01a0c068-03b5-70a2-a68c-a28623e43d13"
STUCK_499 = "issue_stuck:01a0a6a7-d356-7134-9d86-f970cc471e1d:in_progress"
STUCK_512 = "issue_stuck:01a0abce-41ea-7886-bb8c-77da80785671:todo"
STUCK_545 = "issue_stuck:01a0b61b-169e-78e1-a6b5-8d7641bcf205:todo"
RUN_STUCK_545 = "run_stuck:01a0b61b-1695-74ef-b093-e3bbf6c3aa0f"


# 07:00 and 08:00 Asia/Shanghai on the mornings the diagnosis says should have paged someone.
@pytest.mark.parametrize(("now", "expected"), [
    # WAIO-146: 08-18 run blocked (audit c5a7bd974164 hit the 900s timeout); 9-day freeze began.
    ("2026-08-18T23:00:00Z", {RUN_146}),
    ("2026-08-19T00:00:00Z", {RUN_146}),
    # WAIO-499: "Selected model is at capacity"; the issue was then orphaned in in_progress.
    ("2026-09-15T23:00:00Z", {RUN_499}),
    ("2026-09-16T00:00:00Z", {RUN_499, STUCK_499}),
    # WAIO-545 never left todo (9Router gave the agent no tools); 499/512 were already orphaned.
    ("2026-09-18T23:00:00Z", {STUCK_499, STUCK_512}),
    ("2026-09-19T00:00:00Z", {STUCK_499, STUCK_512, STUCK_545, RUN_STUCK_545}),
    # WAIO-559: ingest 02325bea5bc3 failed validation; the ledger deadlock began.
    ("2026-09-20T23:00:00Z", {RUN_559, STUCK_499, STUCK_512, STUCK_545, RUN_STUCK_545}),
    ("2026-09-21T00:00:00Z", {RUN_559, STUCK_499, STUCK_512, STUCK_545, RUN_STUCK_545}),
])
def test_replay_alerts_on_mornings_that_needed_a_human(now: str, expected: set[str]) -> None:
    code, result = run("--multica", "--multica-bin", multica_bin(), "--now", now, *REPLAY)
    assert (code, keys(result), result["errors"]) == (1, expected, [])


@pytest.mark.parametrize("now", [
    "2026-08-17T23:00:00Z", "2026-08-28T23:00:00Z", "2026-09-12T23:00:00Z",
    "2026-09-13T23:00:00Z", "2026-09-14T23:00:00Z",
])
def test_replay_is_quiet_on_healthy_mornings(now: str) -> None:
    code, result = run("--multica", "--multica-bin", multica_bin(), "--now", now, *REPLAY)
    assert (code, result["alerts"], result["errors"]) == (0, [], [])
    assert result["checks"]["multica"]["latest_run"]["status"] == "completed"
    assert result["checks"]["multica"]["checkpoint"]["age_hours"] < 3


@pytest.mark.parametrize(("now", "issue"), [
    ("2026-09-20T12:00:00Z", "WAIO-547"),  # 09-19 run failed; checkpoint written 09-20T10:35:37Z
    ("2026-09-10T13:00:00Z", "WAIO-427"),  # 09-09 run failed; checkpoint written 09-10T12:40:32Z
])
def test_failed_run_recovered_by_a_later_checkpoint_does_not_alert(now: str, issue: str) -> None:
    _code, result = run("--multica", "--multica-bin", multica_bin(), "--now", now, *REPLAY)
    facts = result["checks"]["multica"]
    assert facts["latest_run"]["status"] == "failed"
    assert facts["checkpoint"]["issue"] == issue
    assert not any(k.startswith("latest_run_failed") for k in keys(result))


def test_replay_reports_the_august_checkpoint_freeze() -> None:
    code, result = run("--multica", "--multica-bin", multica_bin(), "--now", "2026-08-22T00:00:00Z", *REPLAY)
    assert code == 1
    assert "checkpoint_stale:2026-08-17T21:10:10Z" in keys(result)
    assert result["checks"]["multica"]["checkpoint"] == {
        "key": "ai_wiki_incremental_checkpoint_v3", "issue": "WAIO-143",
        "completed_at": "2026-08-17T21:10:10Z", "age_hours": 98.8,
    }


def test_snapshot_state_at_capture_time_and_calls_are_read_only(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    code, result = run("--multica", "--multica-bin", multica_bin(), "--now", "2026-09-23T19:03:07Z",
                       env={"FAKE_MULTICA_LOG": str(log)})
    assert code == 1
    assert keys(result) == {
        "checkpoint_stale:2026-09-20T10:35:37Z",
        "issue_stuck:01a0a6a7-d356-7134-9d86-f970cc471e1d:in_progress",  # WAIO-499
        "issue_stuck:01a0abce-41ea-7886-bb8c-77da80785671:todo",  # WAIO-512
        "issue_stuck:01a0b61b-169e-78e1-a6b5-8d7641bcf205:todo",  # WAIO-545
        "run_stuck:01a0b61b-1695-74ef-b093-e3bbf6c3aa0f",
        "latest_run_failed:01a0cab4-a405-7cfd-baff-8dbcdfb7ac69",  # WAIO-587
    }
    assert result["checks"]["multica"]["checkpoint"]["issue"] == "WAIO-547"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert {tuple(c[:2]) for c in calls} <= READ_ONLY
    assert sum(c[:2] == ["issue", "list"] for c in calls) == 3  # 49 issues, 20 per page
    # Timelines are only needed for issues that could be stuck (todo/in_progress now).
    assert sum(c[:2] == ["issue", "timeline"] for c in calls) == 3


def test_reassigned_run_issue_is_fetched_individually(tmp_path: Path) -> None:
    snapshot = json.loads(SNAPSHOT.read_text())
    target = next(i for i in snapshot["issues"] if i["identifier"] == "WAIO-547")
    target["_reassigned"] = True
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    log = tmp_path / "calls.jsonl"
    _code, result = run("--multica", "--multica-bin", multica_bin(path), "--now", "2026-09-23T19:03:07Z",
                        env={"FAKE_MULTICA_LOG": str(log)})
    assert result["checks"]["multica"]["checkpoint"]["issue"] == "WAIO-547"
    assert ["issue", "get", target["id"], "--output", "json"] in [json.loads(x) for x in log.read_text().splitlines()]


def test_unreachable_multica_is_an_error_not_healthy() -> None:
    code, result = run("--multica", "--multica-bin", shlex.join([sys.executable, "-c", "raise SystemExit(3)"]))
    assert code == 2
    assert result["status"] == "error"
    assert result["errors"][0]["check"] == "multica"
    assert result["notify"]["fingerprint"]  # a blind watchdog must still be able to page


# --- maintain ledger --------------------------------------------------------------------------


def write_ledger(tmp_path: Path, now: datetime) -> Path:
    """The 2026-09-23 ledger shape: old versions stuck behind real failed receipts, newer
    versions of the same identities blocked with no attempt, plus the new contract statuses."""
    jobs = {j["id"]: j for j in json.loads((FIXTURES / "writer_jobs_20260923.json").read_text())["jobs"]}
    frozen = tmp_path / "sources"
    frozen.mkdir()

    def evidence(name: str, mtime: datetime) -> str:
        path = frozen / name
        path.write_text(name)
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
        return str(path)

    old = now - timedelta(days=5)
    state = {"version": 1, "endpoint": "https://writer.invalid", "bundle": "solvely-wiki", "sources": [
        {"identity": "solvely/daily/done-source", "sha256": "d" * 64, "status": "done",
         "ingest": [jobs["6f4b6f97e4b2"]], "audit": [jobs["f7dddac6e389"]]},
        {"identity": "solvely/daily/experiment-measurement-20260920", "sha256": jobs["02325bea5bc3"]["sha256"],
         "status": "pending", "path": evidence("em-old", old), "error": "ingest requires repair",
         "ingest": [jobs["02325bea5bc3"], jobs["a658b12f157b"]], "audit": []},
        {"identity": "solvely/daily/h5-checkout-recovery-20260920", "sha256": jobs["3399e2a8cea8"]["sha256"],
         "status": "pending", "path": evidence("h5-old", old), "ingest": [jobs["3399e2a8cea8"]], "audit": []},
        {"identity": "solvely/daily/h5-checkout-recovery-20260920", "sha256": "e" * 64, "status": "pending",
         "path": evidence("h5-new", now - timedelta(hours=10)), "ingest": [], "audit": [],
         "error": "an earlier version of this source is unfinished"},
        {"identity": "solvely/daily/broken", "sha256": "f" * 64, "status": "needs_repair",
         "path": evidence("broken", now - timedelta(hours=2)), "error": "auth: 401", "ingest": [], "audit": []},
        {"identity": "solvely/daily/replaced", "sha256": "a" * 64, "status": "superseded",
         "path": evidence("replaced", old), "ingest": [], "audit": []},
    ]}
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    return path


def test_ledger_flags_stale_pending_and_needs_repair(tmp_path: Path) -> None:
    now = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)
    write_ledger(tmp_path, now)
    code, result = run("--ledger", str(tmp_path), "--now", iso(now))  # a state directory works too
    assert code == 1
    assert keys(result) == {
        "ledger_pending_stale:solvely/daily/experiment-measurement-20260920@95498733626b",
        "ledger_pending_stale:solvely/daily/h5-checkout-recovery-20260920@1fcfb67cec6e",
        "ledger_needs_repair:solvely/daily/broken@ffffffffffff",
    }
    facts = result["checks"]["ledger"]
    assert facts["counts"] == {"done": 1, "pending": 3, "needs_repair": 1, "superseded": 1}
    by_sha = {row["sha256"]: row for row in facts["open"]}
    # Age comes from the frozen evidence (5 days) since it predates the first job 02325bea5bc3.
    assert by_sha["95498733626b"]["age_hours"] == 120.0
    assert by_sha["eeeeeeeeeeee"]["age_hours"] == 10.0  # blocked newer version, still within 48h
    assert by_sha["95498733626b"]["error"] == "ingest requires repair"


def test_ledger_age_falls_back_to_first_job_when_evidence_is_missing(tmp_path: Path) -> None:
    now = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)
    path = write_ledger(tmp_path, now)
    for evidence in (tmp_path / "sources").iterdir():
        evidence.unlink()
    _code, result = run("--ledger", str(path), "--now", iso(now))
    rows = {row["sha256"]: row for row in result["checks"]["ledger"]["open"]}
    assert rows["95498733626b"]["since"] == "2026-09-20T20:07:11Z"  # 02325bea5bc3 created
    assert rows["eeeeeeeeeeee"]["age_hours"] is None
    assert "ledger_pending_stale:solvely/daily/h5-checkout-recovery-20260920@eeeeeeeeeeee" not in keys(result)


def test_unreadable_ledger_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{not json")
    code, result = run("--ledger", str(tmp_path / "state.json"))
    assert code == 2
    assert result["errors"][0]["check"] == "ledger"


# --- writer host ------------------------------------------------------------------------------


def make_bundle(tmp_path: Path, committed_at: str, jobs: list[dict]) -> Path:
    bundle = tmp_path / "solvely-wiki"
    (bundle / ".okf" / "jobs").mkdir(parents=True)
    (bundle / "SCHEMA.md").write_text("# schema\n")
    env = {**os.environ, "GIT_AUTHOR_DATE": committed_at, "GIT_COMMITTER_DATE": committed_at}
    for args in (["init", "-q"], ["add", "SCHEMA.md"],
                 ["-c", "user.name=t", "-c", "user.email=t@local", "commit", "-qm", "audit: ingest 6f4b6f97e4b2"]):
        subprocess.run(["git", "-C", str(bundle), *args], check=True, env=env)
    for job in jobs:
        (bundle / ".okf" / "jobs" / f"{job['id']}.json").write_text(json.dumps(job))
    return bundle


def real_jobs() -> list[dict]:
    return json.loads((FIXTURES / "writer_jobs_20260923.json").read_text())["jobs"]


def test_writer_failures_resolved_by_later_attempts_do_not_alert(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, "2026-09-23T17:42:15Z", real_jobs())
    code, result = run("--bundle", str(bundle), "--now", "2026-09-23T19:00:00Z")
    # Only 3399e2a8cea8 (09-20, h5-checkout-recovery, never retried) is still unresolved.
    assert (code, keys(result)) == (1, {"job_failed:solvely-wiki:3399e2a8cea8"})
    facts = result["checks"]["writer:solvely-wiki"]
    assert {row["id"]: row["resolved_by"] for row in facts["failed_in_window"]} == {
        "a658b12f157b": "6f4b6f97e4b2",  # same sha retried and done
        "71ea85ca9c20": "f7dddac6e389",  # same parent audited and done
        "e94c8b707aea": "f7dddac6e389",
    }
    assert facts["last_commit"]["age_hours"] == 1.3
    assert facts["status_counts"] == {"failed": 5, "done": 2}


def test_writer_alerts_on_unresolved_failures_inside_the_window(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, "2026-09-20T12:00:00Z", real_jobs())
    # 17:20Z: e94c8b707aea had failed and f7dddac6e389 was not yet created.
    code, result = run("--bundle", str(bundle), "--now", "2026-09-23T17:20:00Z")
    assert code == 1
    assert keys(result) == {
        "job_failed:solvely-wiki:71ea85ca9c20", "job_failed:solvely-wiki:e94c8b707aea",
        "job_failed:solvely-wiki:3399e2a8cea8",  # 09-20, outside the 24h listing but never retried
        "bundle_commit_stale:solvely-wiki:" + result["checks"]["writer:solvely-wiki"]["last_commit"]["commit"],
    }
    # 09-21 morning: both 09-20 validation failures were still unresolved.
    _code, result = run("--bundle", str(bundle), "--now", "2026-09-21T00:00:00Z")
    assert {"job_failed:solvely-wiki:02325bea5bc3", "job_failed:solvely-wiki:3399e2a8cea8"} <= keys(result)


@pytest.mark.parametrize(("now", "expected"), [
    # 09-21 evening: both 09-20 failures are past the 24h listing window but nothing retried them.
    ("2026-09-21T21:07:00Z", {"job_failed:solvely-wiki:02325bea5bc3", "job_failed:solvely-wiki:3399e2a8cea8"}),
    # 02325bea5bc3's sha was retried and done on 09-23 (6f4b6f97e4b2); 3399e2a8cea8 never was.
    ("2026-09-27T20:00:00Z", {"job_failed:solvely-wiki:3399e2a8cea8"}),
    # 7 days (--unresolved-failure-hours) after 3399e2a8cea8 failed, the writer stops reporting it.
    ("2026-09-27T21:00:00Z", set()),
])
def test_unretried_writer_failure_alerts_past_the_listing_window(tmp_path: Path, now: str, expected: set) -> None:
    bundle = make_bundle(tmp_path, "2026-09-20T12:00:00Z", real_jobs())
    _code, result = run("--bundle", str(bundle), "--now", now, "--commit-max-age-hours", "1000")
    assert keys(result) == expected
    assert result["checks"]["writer:solvely-wiki"]["failed_in_window"] == []


def test_failure_leaving_the_listing_window_is_not_announced_as_recovery(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    job = next(j for j in real_jobs() if j["id"] == "3399e2a8cea8")  # never retried in production
    bundle = make_bundle(tmp_path, iso(now - timedelta(hours=1)), [])
    job_file = bundle / ".okf" / "jobs" / "3399e2a8cea8.json"
    state = tmp_path / "watchdog.json"
    args = ("--bundle", str(bundle), "--state-file", str(state))

    for failed_hours_ago, action in ((23, "alert"), (47, "unchanged")):  # a day later, still unretried
        at = now - timedelta(hours=failed_hours_ago)
        job_file.write_text(json.dumps({**job, "created": iso(at - timedelta(minutes=4)),
                                        "started": iso(at - timedelta(minutes=4)), "finished": iso(at)}))
        code, result = run(*args)
        assert (code, keys(result), result["notify"]["action"]) == (
            1, {"job_failed:solvely-wiki:3399e2a8cea8"}, action)


def test_writer_reports_queue_depth_and_stuck_jobs(tmp_path: Path) -> None:
    now = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)
    jobs = [
        {"id": "queued000001", "kind": "ingest", "status": "queued", "created": iso(now - timedelta(hours=5))},
        {"id": "queued000002", "kind": "ingest", "status": "queued", "created": iso(now - timedelta(minutes=5))},
        {"id": "running00001", "kind": "audit", "status": "running", "created": iso(now - timedelta(hours=9)),
         "started": iso(now - timedelta(minutes=30))},
    ]
    bundle = make_bundle(tmp_path, iso(now - timedelta(hours=1)), jobs)
    (bundle / ".okf" / "jobs" / "torn.json").write_text("{")
    code, result = run("--bundle", str(bundle), "--now", iso(now))
    assert code == 1
    assert keys(result) == {"job_stuck:solvely-wiki:queued000001:queued"}
    facts = result["checks"]["writer:solvely-wiki"]
    assert (facts["queue_depth"], facts["oldest_queued_age_hours"], facts["unreadable_jobs"]) == (2, 5.0, 1)


def test_missing_job_directory_is_an_error(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, "2026-09-23T17:42:15Z", [])
    (bundle / ".okf" / "jobs").rmdir()
    code, result = run("--bundle", str(bundle))
    assert code == 2
    assert "job directory" in result["errors"][0]["error"]


# --- Feishu notification and deduplication ----------------------------------------------------


class Webhook:
    """Local stand-in for a Feishu custom bot webhook."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.reply = {"code": 0, "msg": "success"}
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                hook.messages.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps(hook.reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/open-apis/bot/v2/hook/test"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def webhook():
    hook = Webhook()
    yield hook
    hook.close()


def test_feishu_alert_is_deduplicated_and_recovery_is_sent_once(tmp_path: Path, webhook: Webhook) -> None:
    ledger = write_ledger(tmp_path, datetime.now(UTC))
    state = tmp_path / "watchdog" / "state.json"
    args = ("--ledger", str(ledger), "--state-file", str(state), "--label", "runtime")
    env = {"AIWIKI_WATCHDOG_FEISHU_WEBHOOK": webhook.url, "AIWIKI_WATCHDOG_FEISHU_SECRET": "s3cret"}

    code, first = run(*args, env=env)
    assert (code, first["notify"]["action"], first["notify"]["sent"]) == (1, "alert", True)
    [message] = webhook.messages
    assert message["msg_type"] == "text"
    text = message["content"]["text"]
    assert text.startswith("【AI Wiki 维护告警】runtime\n1. ")
    # The age is the earlier of real-clock evidence (now - 5 days) and the fixed receipt 3399e2a8cea8
    # created 2026-09-20T20:42:36Z, so it depends on today's date: read it back instead of hardcoding.
    h5_age = next(row["age_hours"] for row in first["checks"]["ledger"]["open"] if row["sha256"] == "1fcfb67cec6e")
    assert h5_age >= 120.0
    assert f"h5-checkout-recovery-20260920 已 pending {h5_age}h（阈值 48h）" in text
    assert "需要人工修复" in text
    expected = hmac.new(f"{message['timestamp']}\ns3cret".encode(), b"", hashlib.sha256).digest()
    assert message["sign"] == base64.b64encode(expected).decode()
    assert webhook.url not in json.dumps(first)

    code, second = run(*args, env=env)
    assert (code, second["notify"]["action"], len(webhook.messages)) == (1, "unchanged", 1)

    data = json.loads(ledger.read_text())
    data["sources"] = [s for s in data["sources"] if s["status"] != "needs_repair"]
    ledger.write_text(json.dumps(data))
    code, changed = run(*args, env=env)
    assert (code, changed["notify"]["action"], len(webhook.messages)) == (1, "alert", 2)
    assert "需要人工修复" not in webhook.messages[-1]["content"]["text"]

    for source in data["sources"]:
        source["status"] = "done"
    ledger.write_text(json.dumps(data))
    code, recovered = run(*args, env=env)
    assert (code, recovered["notify"]["action"], len(webhook.messages)) == (0, "recovery", 3)
    recovery_text = webhook.messages[-1]["content"]["text"]
    assert recovery_text.startswith("【AI Wiki 维护恢复】runtime\n之前的告警已全部解除（始于 ")
    assert json.loads(state.read_text())["fingerprint"] is None

    code, quiet = run(*args, env=env)
    assert (code, quiet["notify"]["action"], len(webhook.messages)) == (0, "unchanged", 3)


def test_rejected_webhook_is_an_error_and_retried_next_run(tmp_path: Path, webhook: Webhook) -> None:
    ledger = write_ledger(tmp_path, datetime.now(UTC))
    state = tmp_path / "state.json.watchdog"
    args = ("--ledger", str(ledger), "--state-file", str(state), "--feishu-webhook", webhook.url)
    webhook.reply = {"code": 19021, "msg": "sign match fail or timestamp is not within one hour from current time"}
    code, result = run(*args)
    assert (code, result["status"], result["notify"]["sent"]) == (2, "error", False)
    assert "19021" in result["notify"]["error"]
    assert not state.exists()

    webhook.reply = {"StatusCode": 0, "StatusMessage": "success"}
    code, result = run(*args)
    assert (code, result["notify"]["action"], result["notify"]["sent"]) == (1, "alert", True)
    assert len(webhook.messages) == 2


def test_malformed_webhook_is_an_error_and_is_not_echoed(tmp_path: Path) -> None:
    ledger = write_ledger(tmp_path, datetime.now(UTC))
    hook = "open.feishu.cn/open-apis/bot/v2/hook/SECRET-HOOK-ID"  # scheme missing
    proc = subprocess.run([sys.executable, str(SCRIPT), "--ledger", str(ledger), "--state-file",
                           str(tmp_path / "s.json"), "--feishu-webhook", hook], capture_output=True, text=True)
    assert proc.returncode == 2
    assert "SECRET-HOOK-ID" not in proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert (result["status"], result["notify"]["sent"]) == ("error", False)
    assert "ValueError" in result["notify"]["error"]
    assert not (tmp_path / "s.json").exists()


@pytest.mark.parametrize("reply", [None, ["ok"]])
def test_non_object_webhook_reply_is_an_error(tmp_path: Path, webhook: Webhook, reply: object) -> None:
    ledger = write_ledger(tmp_path, datetime.now(UTC))
    state = tmp_path / "state.json.watchdog"
    webhook.reply = reply
    code, result = run("--ledger", str(ledger), "--state-file", str(state), "--feishu-webhook", webhook.url)
    assert (code, result["status"], result["notify"]["sent"]) == (2, "error", False)
    assert "unexpected reply" in result["notify"]["error"]
    assert not state.exists()


def test_unwritable_state_file_is_an_error_not_an_alert(tmp_path: Path, webhook: Webhook) -> None:
    ledger = write_ledger(tmp_path, datetime.now(UTC))
    (tmp_path / "not-a-dir").write_text("")
    state = tmp_path / "not-a-dir" / "state.json"  # fails for root too, unlike chmod
    code, result = run("--ledger", str(ledger), "--state-file", str(state), "--feishu-webhook", webhook.url)
    assert (code, result["status"], result["notify"]["sent"]) == (2, "error", True)
    assert "cannot write state file" in result["notify"]["error"]


def load_script():
    spec = importlib.util.spec_from_file_location("maintenance_watchdog", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unexpected_crash_exits_2_without_leaking_the_webhook(tmp_path: Path, monkeypatch, capsys) -> None:
    watchdog = load_script()
    hook = "https://open.feishu.cn/open-apis/bot/v2/hook/SECRET-HOOK-ID"

    def boom(args, result):
        raise RuntimeError(f"bug near {args.feishu_webhook}")

    monkeypatch.setattr(watchdog, "notify", boom)
    code = watchdog.main(["--ledger", str(write_ledger(tmp_path, datetime.now(UTC))),
                          "--state-file", str(tmp_path / "s.json"), "--feishu-webhook", hook])
    out, err = capsys.readouterr()
    assert code == 2
    assert json.loads(out)["errors"] == [{"check": "watchdog", "error": "unexpected RuntimeError: bug near <redacted>"}]
    assert "SECRET-HOOK-ID" not in out + err
    assert "Traceback" in err


def test_unexpected_check_failure_is_an_error_and_other_checks_still_run(tmp_path: Path) -> None:
    ledger = tmp_path / "state.json"
    corrupt = {"identity": "x", "status": "pending", "ingest": [{"created": "0001-01-01T00:00:00+01:00"}]}
    ledger.write_text(json.dumps({"sources": [corrupt]}))  # OverflowError when converted to UTC
    bundle = make_bundle(tmp_path, iso(datetime.now(UTC) - timedelta(hours=1)), [])
    code, result = run("--ledger", str(ledger), "--bundle", str(bundle))
    assert code == 2
    assert result["errors"][0]["check"] == "ledger"
    assert "writer:solvely-wiki" in result["checks"]


def test_old_python_is_refused_with_exit_2(monkeypatch, capsys) -> None:
    watchdog = load_script()  # must stay importable there, or the ImportError would exit 1 (= alert)
    monkeypatch.setattr(sys, "version_info", (3, 10, 19, "final", 0))
    assert watchdog.main(["--ledger", "state.json"]) == 2
    assert "Python 3.11+ required" in json.loads(capsys.readouterr().out)["errors"][0]["error"]


@pytest.mark.parametrize("argv", [
    [],
    ["--ledger", "state.json", "--feishu-webhook", "http://127.0.0.1:9/hook"],
    ["--ledger", "state.json", "--now", "2026-09-19T00:00:00Z", "--state-file", "s.json"],
    ["--ledger", "state.json", "--now", "yesterday"],
])
def test_usage_errors_exit_2(argv: list[str]) -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if not k.startswith("AIWIKI_WATCHDOG_")})
    assert proc.returncode == 2
    assert "error:" in proc.stderr
