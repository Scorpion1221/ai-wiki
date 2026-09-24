#!/usr/bin/env python3
"""Deterministic AI Wiki maintenance watchdog. Stdlib only, read-only, no LLM.

Each check group is opt-in:

  --multica        Multica (via the `multica` CLI): latest autopilot run status/age, age of the
                   newest checkpoint `completed_at` in run-issue metadata, run issues stuck in
                   todo/in_progress, and runs stuck before a terminal state.
  --ledger PATH    `ai-wiki maintain` state.json (or its state directory): pending ages and
                   needs_repair entries.
  --bundle PATH    Writer host bundle (repeatable): last Git commit age, and the worker's job
                   files in <bundle>/.okf/jobs (unresolved failures, queue depth, stuck jobs).

Prints one JSON document. Exit 0 = ok, 1 = alert, 2 = error (a check or the notification
failed, or bad usage). With --feishu-webhook, a short Chinese text message is posted only when
the alert fingerprint changes (deduplicated through --state-file), plus one recovery message
when every alert clears.

--now replays the Multica checks at a past instant: runs created later are ignored, runs that
completed later count as still open, checkpoints written later are ignored, and issue status
is rebuilt from the status_changed timeline. Ledger and writer checks read the files as they
are now and only measure ages from --now.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Not `from datetime import UTC`: an older python3 must still import this file, so main() can
# refuse it with exit 2 instead of an ImportError whose exit 1 would read as "alert".
UTC = timezone.utc  # noqa: UP017

DEFAULT_AUTOPILOT = "5c80732b-67a6-4e33-ba22-c620a94e27c1"
DEFAULT_CHECKPOINT_KEY = "ai_wiki_incremental_checkpoint_v4"
STUCK_ISSUE_STATUSES = ("todo", "in_progress")
CLOSED_ISSUE_STATUSES = ("done", "cancelled", "canceled")
MAX_MESSAGE_LINES = 12


class CheckError(RuntimeError):
    """A check could not read its source; reported as an error, never as healthy."""


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def iso(ts: datetime | None) -> str | None:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def age_h(now: datetime, ts: datetime | None) -> float | None:
    return round((now - ts).total_seconds() / 3600, 1) if ts else None


def short(text: object, limit: int = 80) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def alert(check: str, key: str, message: str) -> dict:
    return {"check": check, "key": key, "message": message}


def run_json(cmd: list[str], *, timeout: float = 60) -> object:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"{shlex.join(cmd[:4])}: {exc}") from None
    if proc.returncode != 0:
        raise CheckError(f"{shlex.join(cmd[:4])} exit {proc.returncode}: {short(proc.stderr or proc.stdout, 300)}")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise CheckError(f"{shlex.join(cmd[:4])}: output is not JSON") from None


# --- Multica -------------------------------------------------------------------------------


def multica(argv: list[str], *args: str) -> object:
    return run_json([*argv, *args, "--output", "json"])


def issue_status_at(issue: dict, changes: list[dict], now: datetime) -> tuple[str | None, datetime | None]:
    """Rebuild an issue's status (and since when) at ``now`` from status_changed activities."""
    dated = sorted(
        ((ts, c) for c in changes if (ts := parse_ts(c.get("created_at")))), key=lambda item: item[0]
    )
    status = issue.get("status")
    if dated:
        status = (dated[0][1].get("details") or {}).get("from") or status
    since = parse_ts(issue.get("created_at"))
    for ts, change in dated:
        if ts > now:
            break
        status = (change.get("details") or {}).get("to") or status
        since = ts
    return status, since


def load_checkpoint(value: object) -> dict | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def check_multica(args: argparse.Namespace, now: datetime) -> tuple[dict, list[dict]]:
    argv = shlex.split(args.multica_bin)
    ap = args.autopilot_id
    runs = multica(argv, "autopilot", "runs", ap, "--limit", str(args.runs_limit))
    runs = [r for r in (runs.get("runs") or []) if (ts := parse_ts(r.get("created_at"))) and ts <= now]
    runs.sort(key=lambda r: parse_ts(r["created_at"]))

    issues: dict[str, dict] = {}
    assignee = (multica(argv, "autopilot", "get", ap).get("autopilot") or {}).get("assignee_id")
    if assignee:  # one listing carries status and metadata for every run issue
        offset = 0
        for _page in range(20):
            page = multica(argv, "issue", "list", "--assignee-id", assignee,
                           "--limit", "100", "--offset", str(offset))
            batch = page.get("issues") or []
            issues.update((i["id"], i) for i in batch if isinstance(i, dict) and i.get("id"))
            if not page.get("has_more") or not batch:
                break
            offset += len(batch)
    for run in runs:  # reassigned issues are not in the assignee listing
        issue_id = run.get("issue_id")
        if issue_id and issue_id not in issues:
            issues[issue_id] = multica(argv, "issue", "get", issue_id)
    run_issues = {r["issue_id"]: issues[r["issue_id"]] for r in runs if r.get("issue_id") in issues}

    alerts: list[dict] = []
    newest = None
    for issue in run_issues.values():
        for key in args.checkpoint_key:
            checkpoint = load_checkpoint((issue.get("metadata") or {}).get(key))
            completed = parse_ts(checkpoint.get("completed_at")) if checkpoint else None
            if completed and completed <= now and (newest is None or completed > newest[0]):
                newest = (completed, key, issue)
    if newest is None:
        checkpoint_fact = None
        alerts.append(alert("multica", "checkpoint_missing",
                            f"最近 {len(runs)} 个 run 的 issue 里没有 {'/'.join(args.checkpoint_key)} checkpoint"))
    else:
        completed, key, issue = newest
        checkpoint_fact = {"key": key, "issue": issue.get("identifier") or issue["id"],
                           "completed_at": iso(completed), "age_hours": age_h(now, completed)}
        if checkpoint_fact["age_hours"] > args.checkpoint_max_age_hours:
            alerts.append(alert("multica", f"checkpoint_stale:{iso(completed)}",
                                f"checkpoint 已 {checkpoint_fact['age_hours']}h 未推进（{checkpoint_fact['issue']} "
                                f"completed_at {iso(completed)}，阈值 {args.checkpoint_max_age_hours:g}h）"))

    states: dict[str, tuple[str | None, datetime | None]] = {}
    for issue_id, issue in run_issues.items():
        changed = max(filter(None, (parse_ts(issue.get("updated_at")), parse_ts(issue.get("last_activity_at")))),
                      default=None)
        if issue.get("status") in STUCK_ISSUE_STATUSES or (changed and changed > now):
            changes = multica(argv, "issue", "timeline", issue_id, "--action", "status_changed")
            states[issue_id] = issue_status_at(issue, changes if isinstance(changes, list) else [], now)
        else:
            states[issue_id] = (issue.get("status"), None)

    def name(issue_id: str) -> str:
        return run_issues[issue_id].get("identifier") or issue_id[:8]

    stuck_issues = []
    for issue_id, (status, since) in states.items():
        age = age_h(now, since)
        if status in STUCK_ISSUE_STATUSES and age is not None and age > args.stuck_hours:
            stuck_issues.append({"issue": name(issue_id), "status": status, "since": iso(since), "age_hours": age})
            alerts.append(alert("multica", f"issue_stuck:{issue_id}:{status}",
                                f"{name(issue_id)} 停在 {status} 已 {age}h（阈值 {args.stuck_hours:g}h）"))

    stuck_runs = []
    for run in runs:
        created, completed = parse_ts(run["created_at"]), parse_ts(run.get("completed_at"))
        if completed is not None and completed <= now:
            continue
        status = run.get("status") if completed is None else "issue_created"  # replay: not yet terminal
        issue_state = states.get(run.get("issue_id"), (None, None))[0]
        age = age_h(now, created)
        if age > args.stuck_hours and issue_state not in CLOSED_ISSUE_STATUSES:
            label = name(run["issue_id"]) if run.get("issue_id") in run_issues else run["id"][:8]
            stuck_runs.append({"run": run["id"], "issue": label, "status": status, "age_hours": age})
            alerts.append(alert("multica", f"run_stuck:{run['id']}",
                                f"autopilot run（{label}）停在 {status} 已 {age}h（阈值 {args.stuck_hours:g}h）"))

    latest_fact = None
    if not runs:
        alerts.append(alert("multica", "runs_missing", f"autopilot {ap[:8]} 没有任何 run"))
    else:
        latest = runs[-1]
        created, completed = parse_ts(latest["created_at"]), parse_ts(latest.get("completed_at"))
        terminal = completed is not None and completed <= now
        status = latest.get("status") if terminal or completed is None else "issue_created"
        label = name(latest["issue_id"]) if latest.get("issue_id") in run_issues else latest["id"][:8]
        latest_fact = {"run": latest["id"], "issue": label, "created_at": iso(created), "status": status,
                       "age_hours": age_h(now, created),
                       "failure_reason": latest.get("failure_reason") if terminal else None}
        if latest_fact["age_hours"] > args.run_max_age_hours:
            alerts.append(alert("multica", f"run_overdue:{latest['id']}",
                                f"autopilot 已 {latest_fact['age_hours']}h 没有新 run"
                                f"（阈值 {args.run_max_age_hours:g}h）"))
        # A failed run that a later checkpoint closed out (manual recovery on the same issue) is healthy.
        if status == "failed" and not (newest and newest[0] >= created):
            alerts.append(alert("multica", f"latest_run_failed:{latest['id']}",
                                f"最新 autopilot run（{label}，{iso(created)}）失败且之后无新 checkpoint："
                                f"{short(latest.get('failure_reason'), 60) or '无原因'}"))

    facts = {"autopilot": ap, "runs_checked": len(runs), "latest_run": latest_fact, "checkpoint": checkpoint_fact,
             "stuck_issues": stuck_issues, "stuck_runs": stuck_runs}
    return facts, alerts


# --- maintain ledger -----------------------------------------------------------------------


def pending_since(entry: dict) -> datetime | None:
    """Earliest evidence of the entry: its first job or the frozen evidence file's mtime."""
    stamps = [parse_ts(job.get("created")) for stage in ("ingest", "audit")
              for job in entry.get(stage) or [] if isinstance(job, dict)]
    frozen = entry.get("path")
    if isinstance(frozen, str) and frozen:
        try:
            stamps.append(datetime.fromtimestamp(Path(frozen).stat().st_mtime, UTC))
        except OSError:
            pass
    return min(filter(None, stamps), default=None)


def check_ledger(args: argparse.Namespace, now: datetime) -> tuple[dict, list[dict]]:
    path = Path(args.ledger).expanduser()
    if path.is_dir():
        path = path / "state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError(f"cannot read ledger {path}: {exc}") from None
    sources = state.get("sources") if isinstance(state, dict) else None
    if not isinstance(sources, list):
        raise CheckError(f"ledger {path} has no sources list")

    counts: dict[str, int] = {}
    open_entries, alerts = [], []
    for entry in sources:
        status = entry.get("status") or "pending"
        counts[status] = counts.get(status, 0) + 1
        if status in ("done", "superseded"):
            continue
        identity, sha = str(entry.get("identity")), str(entry.get("sha256") or "")[:12]
        since = pending_since(entry)
        row = {"identity": identity, "sha256": sha, "status": status, "since": iso(since),
               "age_hours": age_h(now, since), "retry_at": (entry.get("retry") or {}).get("after"),
               "submitting": entry.get("submitting"), "error": short(entry.get("error"), 200) or None}
        open_entries.append(row)
        if status == "needs_repair":
            alerts.append(alert("ledger", f"ledger_needs_repair:{identity}@{sha}",
                                f"ledger {identity} 需要人工修复：{short(entry.get('error'), 60) or status}"))
        elif row["age_hours"] is not None and row["age_hours"] > args.pending_max_age_hours:
            alerts.append(alert("ledger", f"ledger_pending_stale:{identity}@{sha}",
                                f"ledger {identity} 已 pending {row['age_hours']}h"
                                f"（阈值 {args.pending_max_age_hours:g}h）"))
    open_entries.sort(key=lambda r: r["since"] or "")
    facts = {"path": str(path), "counts": counts, "open": open_entries,
             "writer_retry_after": (state.get("writer_retry") or {}).get("after")}
    return facts, alerts


# --- writer host ---------------------------------------------------------------------------


def check_bundle(bundle: Path, args: argparse.Namespace, now: datetime) -> tuple[dict, list[dict]]:
    name = bundle.name
    check = f"writer:{name}"
    try:
        proc = subprocess.run(
            ["git", "-C", str(bundle), "log", "-1", f"--before={iso(now)}", "--format=%H %cI", "--", "."],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"git log in {bundle}: {exc}") from None
    if proc.returncode != 0:
        raise CheckError(f"git log in {bundle} exit {proc.returncode}: {short(proc.stderr, 300)}")
    alerts = []
    commit, _, committed_at = proc.stdout.strip().partition(" ")
    committed = parse_ts(committed_at)
    commit_fact = {"commit": commit[:12] or None, "committed_at": iso(committed), "age_hours": age_h(now, committed)}
    if committed is None:
        alerts.append(alert(check, f"bundle_commit_missing:{name}", f"writer {name}：bundle 没有任何提交"))
    elif commit_fact["age_hours"] > args.commit_max_age_hours:
        alerts.append(alert(check, f"bundle_commit_stale:{name}:{commit[:12]}",
                            f"writer {name}：bundle 已 {commit_fact['age_hours']}h 没有新提交"
                            f"（{commit[:12]}，阈值 {args.commit_max_age_hours:g}h）"))

    jobs_dir = bundle / ".okf" / "jobs"
    if not jobs_dir.is_dir():
        raise CheckError(f"job directory {jobs_dir} does not exist")
    jobs, unreadable = [], 0
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable += 1
            continue
        if isinstance(job, dict) and (created := parse_ts(job.get("created"))) and created <= now:
            jobs.append((created, job))

    def subject(job: dict) -> tuple:
        kind = job.get("kind", "ingest")
        return kind, job.get("parent_job") if kind == "audit" else job.get("sha256")

    counts: dict[str, int] = {}
    for _created, job in jobs:
        counts[str(job.get("status"))] = counts.get(str(job.get("status")), 0) + 1
    window_start = now - timedelta(hours=args.failed_window_hours)
    # An unretried failure keeps alerting after it leaves the listing window; otherwise it would
    # silently drop out and read as a recovery while nothing was fixed.
    alert_start = now - timedelta(hours=max(args.failed_window_hours, args.unresolved_failure_hours))
    failed = []
    for created, job in jobs:
        finished = parse_ts(job.get("finished")) or created
        if job.get("status") != "failed" or not alert_start <= finished <= now:
            continue
        # A later attempt on the same source (ingest) or parent (audit) that is done or in flight resolves it.
        retry = next((j for c, j in jobs if c > created and subject(j) == subject(job)
                      and j.get("status") in ("done", "queued", "running")), None)
        failure = job.get("failure") if isinstance(job.get("failure"), dict) else {}
        row = {"id": job.get("id"), "kind": job.get("kind", "ingest"), "finished": iso(finished),
               "class": failure.get("class"), "error": short(job.get("error"), 120),
               "resolved_by": retry.get("id") if retry else None}
        if finished >= window_start:
            failed.append(row)
        if retry is None:
            reason = row["class"] or short(job.get("error"), 60) or "unknown"
            alerts.append(alert(check, f"job_failed:{name}:{row['id']}",
                                f"writer {name}：{row['kind']} job {row['id']} 于 {row['finished']} 失败且未重试成功"
                                f"（{reason}）"))

    queued = sorted(created for created, job in jobs if job.get("status") == "queued")
    stuck = []
    for created, job in jobs:
        status = job.get("status")
        if status not in ("queued", "running"):
            continue
        since = (parse_ts(job.get("started")) or created) if status == "running" else created
        age = age_h(now, since)
        if age > args.stuck_hours:
            stuck.append({"id": job.get("id"), "status": status, "age_hours": age})
            alerts.append(alert(check, f"job_stuck:{name}:{job.get('id')}:{status}",
                                f"writer {name}：job {job.get('id')} 停在 {status} 已 {age}h"
                                f"（阈值 {args.stuck_hours:g}h）"))
    facts = {"bundle": str(bundle), "last_commit": commit_fact, "jobs": len(jobs), "unreadable_jobs": unreadable,
             "status_counts": counts, "queue_depth": len(queued),
             "oldest_queued_age_hours": age_h(now, queued[0]) if queued else None, "stuck_jobs": stuck,
             "failed_in_window": failed, "failed_window_hours": args.failed_window_hours,
             "unresolved_failure_hours": args.unresolved_failure_hours}
    return facts, alerts


# --- notification --------------------------------------------------------------------------


def render(kind: str, label: str, result: dict, previous: dict) -> str:
    title = "【AI Wiki 维护恢复】" if kind == "recovery" else "【AI Wiki 维护告警】"
    lines = [f"{title}{label}".rstrip()]
    if kind == "recovery":
        lines.append(f"之前的告警已全部解除（始于 {previous.get('alerting_since') or '未知'}）。")
    else:
        items = [a["message"] for a in result["alerts"]]
        items += [f"检查失败 {e['check']}：{short(e['error'], 100)}" for e in result["errors"]]
        lines += [f"{i}. {text}" for i, text in enumerate(items[:MAX_MESSAGE_LINES], 1)]
        if len(items) > MAX_MESSAGE_LINES:
            lines.append(f"……另有 {len(items) - MAX_MESSAGE_LINES} 条，详见 watchdog JSON 输出")
    lines.append(f"检查时间 {result['now']}")
    return "\n".join(lines)


def redact(text: str, *secrets: str | None) -> str:
    for secret in filter(None, secrets):
        text = text.replace(secret, "<redacted>")
    return text


def post_feishu(url: str, secret: str | None, text: str) -> None:
    body: dict = {"msg_type": "text", "content": {"text": text}}
    if secret:  # Feishu custom bot signature: HMAC-SHA256 keyed by "timestamp\nsecret" over an empty message
        stamp = str(int(time.time()))
        digest = hmac.new(f"{stamp}\n{secret}".encode(), b"", hashlib.sha256).digest()
        body.update(timestamp=stamp, sign=base64.b64encode(digest).decode())
    try:  # Request() itself raises ValueError for a malformed URL
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            reply = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        reason = redact(str(getattr(exc, "reason", exc)), url, secret)  # never echo the webhook URL
        raise CheckError(f"feishu webhook failed: {type(exc).__name__}: {reason}") from None
    if not isinstance(reply, dict):
        raise CheckError(f"feishu webhook returned an unexpected reply: {short(json.dumps(reply), 120)}")
    code = reply.get("code", reply.get("StatusCode", 0))
    if code != 0:
        raise CheckError(f"feishu webhook rejected the message: code {code} {short(reply.get('msg'), 120)}")


def notify(args: argparse.Namespace, result: dict) -> dict:
    keys = sorted([a["key"] for a in result["alerts"]] + [f"error:{e['check']}" for e in result["errors"]])
    fingerprint = hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16] if keys else None
    state_path = Path(args.state_file).expanduser() if args.state_file else None
    previous: dict = {}
    if state_path:
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            pass  # missing or unreadable state: re-announce rather than stay silent
    if fingerprint == previous.get("fingerprint"):
        action = "unchanged"
    elif fingerprint:
        action = "alert"
    else:
        action = "recovery" if previous.get("fingerprint") else "none"
    outcome: dict = {"fingerprint": fingerprint, "action": action, "sent": False}
    if action in ("alert", "recovery") and args.feishu_webhook:
        try:
            post_feishu(args.feishu_webhook, args.feishu_secret, render(action, args.label, result, previous))
        except CheckError as exc:
            outcome["error"] = str(exc)
            return outcome  # keep the old state so the next run retries the message
        outcome["sent"] = True
    if state_path:
        alerting_since = previous.get("alerting_since") if previous.get("fingerprint") else result["now"]
        new_state = {"fingerprint": fingerprint, "keys": keys, "updated_at": result["now"],
                     "alerting_since": alerting_since if fingerprint else None}
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, state_path)
        except OSError as exc:  # dedup cannot advance: surface it as an error (exit 2), not as an alert
            outcome["error"] = f"cannot write state file {state_path}: {exc}"
    return outcome


# --- entry point ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("checks")
    g.add_argument("--multica", action="store_true", help="check the Multica autopilot, issues and checkpoint")
    g.add_argument("--ledger", help="maintain state.json, or the state directory that holds it")
    g.add_argument("--bundle", action="append", default=[], help="writer bundle directory (repeatable)")
    m = p.add_argument_group("multica")
    m.add_argument("--autopilot-id", default=DEFAULT_AUTOPILOT)
    m.add_argument("--multica-bin", default="multica", help="command prefix, e.g. 'multica --profile prod'")
    m.add_argument("--runs-limit", type=int, default=30, help="autopilot runs to inspect (default 30)")
    m.add_argument("--checkpoint-key", action="append",
                   help=f"issue metadata key (repeatable; default {DEFAULT_CHECKPOINT_KEY})")
    t = p.add_argument_group("thresholds (hours)")
    t.add_argument("--checkpoint-max-age-hours", type=float, default=30)
    t.add_argument("--run-max-age-hours", type=float, default=26, help="no new autopilot run for this long")
    t.add_argument("--stuck-hours", type=float, default=3, help="issues, runs and writer jobs")
    t.add_argument("--pending-max-age-hours", type=float, default=48)
    t.add_argument("--commit-max-age-hours", type=float, default=48)
    t.add_argument("--failed-window-hours", type=float, default=24, help="writer failures listed in the output")
    t.add_argument("--unresolved-failure-hours", type=float, default=168,
                   help="keep alerting on a writer failure with no later attempt for this long (default 7 days)")
    n = p.add_argument_group("notification")
    n.add_argument("--state-file", help="dedup state; required with --feishu-webhook")
    n.add_argument("--feishu-webhook", default=os.environ.get("AIWIKI_WATCHDOG_FEISHU_WEBHOOK"),
                   help="Feishu custom bot URL (default: $AIWIKI_WATCHDOG_FEISHU_WEBHOOK)")
    n.add_argument("--feishu-secret", default=os.environ.get("AIWIKI_WATCHDOG_FEISHU_SECRET"),
                   help="signing secret for bots with signature check (default: $AIWIKI_WATCHDOG_FEISHU_SECRET)")
    n.add_argument("--label", default=socket.gethostname(), help="shown in the message title (default: hostname)")
    p.add_argument("--now", help="evaluate at this RFC 3339 instant (historical replay; never notifies)")
    return p


def fail(now: datetime, error: str) -> int:
    print(json.dumps({"status": "error", "now": iso(now), "alerts": [],
                      "errors": [{"check": "watchdog", "error": error}]}, ensure_ascii=False, indent=2))
    return 2


def main(argv: list[str] | None = None) -> int:
    # Installed standalone under the host's python3. 3.10's fromisoformat rejects Multica's 5-digit
    # fractions (…:37.12345Z), so those timestamps would be skipped silently.
    if sys.version_info < (3, 11):  # noqa: UP036
        return fail(datetime.now(UTC), f"Python 3.11+ required, found {sys.version.split()[0]}")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return watch(parser, args)
    except Exception as exc:  # any crash must exit 2: exit 1 means "alert delivered" to systemd
        secrets = (args.feishu_webhook, args.feishu_secret)
        print(redact(traceback.format_exc(), *secrets), file=sys.stderr)
        return fail(datetime.now(UTC), redact(f"unexpected {type(exc).__name__}: {exc}", *secrets))


def watch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if not (args.multica or args.ledger or args.bundle):
        parser.error("enable at least one check: --multica, --ledger or --bundle")
    if args.feishu_webhook and not args.state_file:
        parser.error("--feishu-webhook requires --state-file for deduplication")
    now = datetime.now(UTC)
    if args.now:
        now = parse_ts(args.now)
        if now is None:
            parser.error("--now must be an RFC 3339 timestamp")
        if args.feishu_webhook or args.state_file:
            parser.error("--now is a dry replay; do not combine it with --feishu-webhook or --state-file")
    args.checkpoint_key = args.checkpoint_key or [DEFAULT_CHECKPOINT_KEY]

    result: dict = {"status": "ok", "now": iso(now), "alerts": [], "errors": [], "checks": {}}
    plan = []
    if args.multica:
        plan.append(("multica", lambda: check_multica(args, now)))
    if args.ledger:
        plan.append(("ledger", lambda: check_ledger(args, now)))
    for bundle in args.bundle:
        path = Path(bundle).expanduser().resolve()
        plan.append((f"writer:{path.name}", lambda path=path: check_bundle(path, args, now)))
    for name, check in plan:
        try:
            facts, alerts = check()
        except CheckError as exc:
            result["errors"].append({"check": name, "error": str(exc)})
            continue
        except Exception as exc:  # unexpected data or a bug: report it and still run the other checks
            result["errors"].append({"check": name, "error": f"unexpected data: {type(exc).__name__}: {exc}"})
            continue
        result["checks"][name] = facts
        result["alerts"].extend(alerts)

    result["notify"] = notify(args, result)
    if result["errors"] or result["notify"].get("error"):
        result["status"] = "error"
    elif result["alerts"]:
        result["status"] = "alert"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"ok": 0, "alert": 1, "error": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
