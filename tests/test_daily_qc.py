"""Daily quality check: one day of a writer's receipts graded against the owner's expectations.
The bundles are shaped after production on 2026-10-29 (a full 240-minute run) and 2026-10-30
(a run that stopped after 14 items)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "daily_qc.py"
MAINTAINER, AUDITOR = "process:ai-wiki-maintainer", "process:ai-wiki-auditor"
NOW = datetime(2026, 10, 30, 2, 30, tzinfo=UTC)  # 10:30 CST
RUN = "01a188d1-fb58-752f-9ce2-a109e50b32de"
AUDIT_RUN = "01a189e4-92d8-7646-a07f-7246dae2b041"


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def at(hours: float) -> str:
    """An instant ``hours`` before NOW."""
    return iso(NOW - timedelta(hours=hours))


class Bundle:
    def __init__(self, root: Path) -> None:
        self.root = root
        for sub in ("maint/items", "jobs", "triage"):
            (root / ".okf" / sub).mkdir(parents=True)
        self.n = 0

    def item(self, status: str, *, created: str, kind: str = "issue", resolution: dict | None = None,
             history: list[dict] | None = None, reopened: list[dict] | None = None) -> str:
        self.n += 1
        item_id = f"it_{self.n:012x}"
        folder = self.root / ".okf" / "maint" / "items" / item_id
        folder.mkdir()
        (folder / "item.json").write_text(json.dumps({
            "id": item_id, "status": status, "origin": {"kind": kind}, "topic_key": f"t{self.n}", "item_key": "k",
            "priority": 40, "brief": "", "files": [], "versions": [], "created_at": created, "updated_at": created,
            "attempts": {"started": 1, "counted": len(history or []), "history": history or []},
            "resolution": resolution, **({"reopened": reopened} if reopened else {})}))
        return item_id

    def close(self, status: str, hours: float, *, reason: str | None = None, run: str = RUN) -> str:
        return self.item(status, created=at(hours + 30), resolution={
            "outcome": status, "reason": reason, "by": MAINTAINER, "run": run, "at": at(hours)})

    def job(self, job_id: str, **fields) -> None:
        (self.root / ".okf" / "jobs" / f"{job_id}.json").write_text(json.dumps({"id": job_id, **fields}))

    def changeset(self, job_id: str, hours: float, status: str = "done") -> None:
        self.job(job_id, kind="ingest", mode="changeset", status=status, principal=MAINTAINER, run=RUN,
                 created=at(hours), finished=at(hours - 0.01))

    def audit(self, job_id: str, hours: float, verdicts: list[str]) -> None:
        self.job(job_id, kind="audit", mode="changeset", status="done", principal=AUDITOR, run=AUDIT_RUN,
                 created=at(hours), finished=at(hours - 0.01),
                 reviews=[{"path": f"m/{i}.md", "verdict": v, "outcome": v} for i, v in enumerate(verdicts)])

    def triage(self, review_id: str, hours: float, verdict: str, claims: int = 0) -> None:
        (self.root / ".okf" / "triage" / f"tr_{review_id}.json").write_text(json.dumps({
            "id": f"tr_{review_id}", "verdict": verdict, "missed": [{"claim": "c"}] * claims, "principal": AUDITOR,
            "run": AUDIT_RUN, "status": "done", "at": at(hours)}))


def full_day(bundle: Bundle) -> None:
    """10-29: 02:11-05:58 CST, 77 curated / 35 skipped / 38 duplicate, all 77 first pass; the
    auditor reviewed 100 concepts (35 verified) and 20 triage items (10 missed)."""
    for i in range(77):
        bundle.close("curated", 8.0 - i * 0.049)
        bundle.changeset(f"c{i:011d}", 8.0 - i * 0.049)
    for i in range(35):
        bundle.close("skipped", 7.0 - i * 0.05, reason="no_durable_knowledge")
    for i in range(38):
        bundle.close("duplicate", 6.0 - i * 0.05, reason="duplicate_of:x.md")
    for i in range(20):
        bundle.audit(f"a{i:011d}", 3.4 - i * 0.05, ["verified"] * (2 if i < 15 else 1) + ["corrected"] * 2
                     + ["unverified"] * (1 if i < 15 else 2))
    for i in range(20):
        bundle.triage(f"{i:012x}", 1.6, "missed" if i < 10 else "upheld", claims=1 if i < 10 else 0)


def qc(bundle: Bundle, *args: str, env: dict | None = None) -> tuple[int, dict]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("AIWIKI_WATCHDOG_")}
    proc = subprocess.run([sys.executable, str(SCRIPT), "--bundle", str(bundle.root), *args],
                          capture_output=True, text=True, env={**base, **(env or {})}, timeout=120)
    assert proc.stdout, proc.stderr
    return proc.returncode, json.loads(proc.stdout)


def grades(result: dict) -> dict[str, str]:
    return {row["check"]: row["grade"] for row in result["rows"]}


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    return Bundle(tmp_path / "solvely-wiki")


def test_a_full_run_meets_the_bar_and_the_misses_are_flagged(bundle: Bundle) -> None:
    full_day(bundle)
    code, result = qc(bundle, "--now", iso(NOW))
    assert code == 0 and result["errors"] == []
    m, a = result["facts"]["maintainer"], result["facts"]["auditor"]
    assert m["runs"] == [RUN] and m["closed"] == 150 and m["changesets"] == 77 and m["span_minutes"] >= 180
    assert m["outcomes"] == {"curated": 77, "skipped": 35, "duplicate": 38}
    assert a["concepts"] == 100 and a["verdicts"]["verified"] == 35 and a["triage_verdicts"] == {"missed": 10,
                                                                                                "upheld": 10}
    assert grades(result) == {"运行时长": "ok", "处理条目": "ok", "首次过 gate": "ok", "跳过+合并占比": "ok",
                              "概念审查": "ok", "通过率": "warn", "triage 漏判率": "warn"}
    assert result["grade"] == "warn"  # a replay grades only the day's work, never the queue as it is now


def test_a_run_that_stops_early_is_bad_unless_the_queue_is_empty(bundle: Bundle) -> None:
    for i in range(14):  # 10-30: 14 items in 39 minutes, 294 still ready
        bundle.close("curated", 7.9 - i * 0.05)
        bundle.changeset(f"c{i:011d}", 7.9 - i * 0.05)
    for i in range(30):
        bundle.item("ready", created=at(80 if i < 8 else 10))
    code, result = qc(bundle)
    by = {row["check"]: row for row in result["rows"]}
    assert by["运行时长"]["grade"] == "bad" and by["处理条目"]["grade"] == "bad" and result["grade"] == "bad"
    assert by["等待超 72h"]["grade"] == "warn" and by["等待超 72h"]["value"].startswith("8")
    assert by["运行"]["grade"] == "bad" and by["运行"]["area"] == "审计"  # no auditor receipt that day

    for path in (bundle.root / ".okf" / "maint" / "items").glob("*/item.json"):  # the queue drained
        item = json.loads(path.read_text())
        if item["status"] == "ready":
            path.parent.joinpath("item.json").unlink()
            path.parent.rmdir()
    _code, result = qc(bundle)
    assert grades(result)["运行时长"] == "ok" and grades(result)["处理条目"] == "ok"


def test_a_close_a_triage_review_reopened_still_counts_for_its_day(bundle: Bundle) -> None:
    first = {"outcome": "duplicate", "reason": "duplicate_of:x.md", "by": MAINTAINER, "run": RUN, "at": at(7)}
    bundle.item("ready", created=at(40), reopened=[{"by": AUDITOR, "at": at(1), "from": "duplicate",
                                                    "resolution": first, "reason": "triage_missed"}])
    _code, result = qc(bundle, "--now", iso(NOW))
    assert result["facts"]["maintainer"]["outcomes"] == {"duplicate": 1}


def test_members_waiting_and_needs_human_are_bad(bundle: Bundle) -> None:
    bundle.item("ready", created=at(30), kind="member")
    bundle.item("needs_human", created=at(50))
    by = {row["check"]: row["grade"] for row in qc(bundle)[1]["rows"]}
    assert by["成员投递"] == "bad" and by["needs_human"] == "bad"


def test_thresholds_are_flags(bundle: Bundle) -> None:
    full_day(bundle)
    _code, result = qc(bundle, "--now", iso(NOW), "--min-verified-pct", "30", "--max-miss-pct", "60")
    assert result["grade"] == "ok"


class Server:
    """The writer's /maint/status and the mirror's /health, with the token checked."""

    def __init__(self, token: str, revision: str) -> None:
        self.requests: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                server.requests.append(self.path)
                ok = self.headers.get("Authorization") == f"Bearer {token}"
                body = ({"triage": {"backlog": 71}, "audit": {"pending": 3, "oldest_finished": at(80)}}
                        if self.path.startswith("/maint/status") else
                        {"git_revision": revision, "by_trust": {"machine-confirmed": 70, "unverified": 30}})
                data = json.dumps(body if ok else {"detail": "no"}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()


def git_bundle(bundle: Bundle) -> str:
    env = {**os.environ, "GIT_AUTHOR_DATE": at(1), "GIT_COMMITTER_DATE": at(1)}
    (bundle.root / "SCHEMA.md").write_text("# schema\n")
    for args in (["init", "-q"], ["add", "SCHEMA.md"],
                 ["-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(bundle.root), *args], check=True, env=env)
    return subprocess.run(["git", "-C", str(bundle.root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def test_the_writer_and_mirror_rows_and_a_lagging_mirror(bundle: Bundle) -> None:
    head = git_bundle(bundle)
    token = "reader-" + "token"
    server = Server(token, head)
    try:
        code, result = qc(bundle, "--writer-url", server.url, "--mirror-url", server.url,
                          env={"AIWIKI_WATCHDOG_TOKEN": token})
        assert code == 0 and result["errors"] == []
        by = {row["check"]: row for row in result["rows"]}
        assert by["triage 积压"]["grade"] == "warn" and by["审计 backlog"]["grade"] == "warn"
        assert by["镜像同步"]["grade"] == "ok" and by["未验证概念"]["grade"] == "warn"
        assert token not in json.dumps(result)

        lagging = Server(token, "0" * 40)
        _code, result = qc(bundle, "--mirror-url", lagging.url, env={"AIWIKI_WATCHDOG_TOKEN": token})
        assert grades(result)["镜像同步"] == "bad"
        code, result = qc(bundle, "--writer-url", server.url, env={"AIWIKI_WATCHDOG_TOKEN": "wrong"})
        assert code == 2 and result["errors"][0]["source"] == "writer" and "401" in result["errors"][0]["error"]
    finally:
        server.http.shutdown()


def test_the_card_is_one_message_coloured_by_the_worst_row(bundle: Bundle) -> None:
    spec = importlib.util.spec_from_file_location("daily_qc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    full_day(bundle)
    args = module.build_parser().parse_args(["--bundle", str(bundle.root), "--now", iso(NOW)])
    report = module.report(args)
    card = module.render_card(report, "bwg")
    assert card["header"]["template"] == "yellow" and "有偏离" in card["header"]["title"]["content"]
    text = "\n".join(e["content"] for e in card["body"]["elements"] if e["tag"] == "markdown")
    assert "⚠️ **triage 漏判率**：10/20（50.0%，10 处遗漏）" in text and "（预期 ≤ 20%" in text
    assert "10-29 10:30 → 10-30 10:30" in text


def test_a_missing_bundle_is_an_error_not_a_clean_day(tmp_path: Path) -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT), "--bundle", str(tmp_path / "nope")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and json.loads(proc.stdout)["grade"] is None
