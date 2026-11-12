#!/usr/bin/env python3
"""Daily AI Wiki quality check: did yesterday's maintenance do what it should? Stdlib only,
read-only, no LLM.

The watchdog (maintenance_watchdog.py) pages when maintenance stops; this report grades the
work of one day against what the owner expects, from the writer's own receipts on its host:

  maintainer   the run's span (from its lease-holding writes: item closes, changeset jobs),
               items it closed and how (curated, skipped, duplicate, parked), first-pass gate
               (changesets accepted vs rejected for its work items).
  auditor      concept reviews (verified / corrected / unverified) and triage reviews of the
               maintainer's skips and duplicates (upheld / missed), from audit jobs and
               .okf/triage receipts.
  queue        ready items now and their age, members waiting, needs_human, intake that day,
               the triage backlog and reopened items (GET /maint/status via --writer-url).
  wiki         the published concepts' trust split and the mirror serving the writer's head.

"The day" is a window of --hours (24) ending at the run time (default: now), so a 10:30 CST
run covers the 02:00 maintainer and the 07:00 auditor of that morning. --now replays a past
window from the receipts' timestamps; the queue and wiki rows describe the present, so a replay
leaves them out. Each expectation is a
row: ok, warn or bad, with the number it read and the threshold. Thresholds are flags, so the
owner tunes the bar without code.

Prints one JSON document whose ``card`` is a Feishu interactive card (schema 2.0), green when
every row is ok, yellow with a warn, red with a bad. Delivery is the caller's: the owner's Hermes
runs this over ssh and sends the card to a private chat with its own bot, so no chat credential
lives on the writer. Exit 0 = report made (whatever the grade), 2 = a source could not be read.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc  # noqa: UP017 - an older python3 must still import this file
CST = timezone(timedelta(hours=8))
MAINTAINER = "process:ai-wiki-maintainer"
AUDITOR = "process:ai-wiki-auditor"
CLOSED = ("curated", "skipped", "duplicate", "split", "needs_access", "needs_conversion")
GRADES = {"ok": "✅", "warn": "⚠️", "bad": "🔴"}
TEMPLATE = {"ok": "green", "warn": "yellow", "bad": "red"}


class SourceError(RuntimeError):
    """A source could not be read: the report is incomplete, never silently healthy."""


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


def cst(ts: datetime | None) -> str:
    return ts.astimezone(CST).strftime("%m-%d %H:%M") if ts else "未知"


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def within(ts: datetime | None, start: datetime, end: datetime) -> bool:
    return ts is not None and start <= ts < end


def pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


# --- sources --------------------------------------------------------------------------------


def load_items(bundle: Path) -> list[dict]:
    root = bundle / ".okf" / "maint" / "items"
    if not root.is_dir():
        raise SourceError(f"{root} does not exist")
    items = []
    for path in sorted(root.glob("it_*/item.json")):
        item = read_json(path)
        if item and isinstance(item.get("status"), str) and isinstance(item.get("attempts"), dict):
            items.append(item)
    return items


def load_jobs(bundle: Path) -> list[dict]:
    root = bundle / ".okf" / "jobs"
    if not root.is_dir():
        raise SourceError(f"{root} does not exist")
    return [job for path in sorted(root.glob("*.json")) if (job := read_json(path))]


def load_triage(bundle: Path) -> list[dict]:
    return [r for path in sorted((bundle / ".okf" / "triage").glob("tr_*.json")) if (r := read_json(path))]


def writer_get(url: str, token: str, route: str, bundle: str) -> dict:
    full = f"{url.rstrip('/')}{route}?{urllib.parse.urlencode({'bundle': bundle})}"
    request = urllib.request.Request(full, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - an operator-set URL
            value = json.loads(response.read())
    except (OSError, ValueError) as exc:
        raise SourceError(f"GET {route}?bundle={bundle}: {' '.join(str(exc).split())[:160]}") from None
    if not isinstance(value, dict):
        raise SourceError(f"GET {route}?bundle={bundle}: not a JSON object")
    return value


def git(bundle: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(bundle), *args], capture_output=True, text=True,
                              timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_head(bundle: Path) -> str | None:
    return git(bundle, "rev-parse", "HEAD") or None


def mirror_lag(bundle: Path, served: str | None, head: str | None, now: datetime) -> tuple[float | None, str]:
    """(minutes since the oldest writer commit the mirror does not serve yet, why there is no number).
    0 when it serves HEAD. The mirror pulls every 5 minutes, so a commit made since its last pull is
    lag, not drift (2026-11-12 10:10Z: the card went red on a 2-minute-old commit; the next pull
    matched). Commits are walked along first parents, so a merge counts from its own date."""
    if not head:
        return None, "读不到 writer HEAD"
    if not served:
        return None, "镜像没报版本"
    if served == head:
        return 0.0, ""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", served):
        return None, "镜像版本不是完整 commit"
    if git(bundle, "merge-base", "--is-ancestor", served, head) is None:
        return None, "不在 writer 历史上"
    stamps = git(bundle, "log", "--first-parent", "--format=%cI", f"{served}..{head}")
    oldest = min((ts for ts in map(parse_ts, (stamps or "").split()) if ts), default=None)
    if oldest is None:
        return None, "读不到落后的提交"
    return round(max((now - oldest).total_seconds(), 0) / 60, 1), ""


# --- the day's facts ------------------------------------------------------------------------


def maintainer_facts(items: list[dict], jobs: list[dict], start: datetime, end: datetime) -> dict:
    """What maintainer runs did in the window: closes, parks and changesets, grouped by run."""
    closes = []
    for item in items:
        # A close a triage review reopened since is kept in ``reopened``: it was still that day's work.
        earlier = [(entry.get("from"), entry.get("resolution")) for entry in item.get("reopened") or []
                   if isinstance(entry, dict) and isinstance(entry.get("resolution"), dict)]
        for status, resolution in [*earlier, (item["status"], item.get("resolution"))]:
            resolution = resolution if isinstance(resolution, dict) else {}
            at = parse_ts(resolution.get("at"))
            if status in CLOSED and resolution.get("by") == MAINTAINER and within(at, start, end):
                closes.append((resolution.get("run"), status, at, item))
    parks = [(h.get("run"), h.get("class"), parse_ts(h.get("at")), item) for item in items
             for h in item["attempts"].get("history") or [] if isinstance(h, dict)
             and within(parse_ts(h.get("at")), start, end) and h.get("class") != "interrupted"]
    changesets = [job for job in jobs if job.get("kind") == "ingest" and job.get("mode") == "changeset"
                  and job.get("principal") == MAINTAINER and within(parse_ts(job.get("created")), start, end)]
    stamps = [at for _r, _s, at, _i in closes] + [at for _r, _c, at, _i in parks if at] + [
        ts for job in changesets for ts in (parse_ts(job.get("created")), parse_ts(job.get("finished"))) if ts]
    runs = sorted({run for run, *_ in closes} | {run for run, *_ in parks} | {job.get("run") for job in changesets}
                  - {None})
    outcomes = Counter(status for _r, status, _a, _i in closes)
    reasons = Counter((item.get("resolution") or {}).get("reason") for _r, status, _a, item in closes
                      if status == "skipped")
    accepted = [job for job in changesets if job.get("status") == "done"]
    rejected = [job for job in changesets if job.get("status") in ("rejected", "failed")]
    return {
        "runs": runs, "first_write": iso(min(stamps, default=None)), "last_write": iso(max(stamps, default=None)),
        "span_minutes": round((max(stamps) - min(stamps)).total_seconds() / 60) if stamps else 0,
        "closed": sum(outcomes.values()), "outcomes": dict(outcomes), "skip_reasons": dict(reasons),
        "parked": Counter(cls for _r, cls, _a, _i in parks), "changesets": len(accepted),
        "changesets_rejected": len(rejected),
    }


def auditor_facts(jobs: list[dict], triage: list[dict], start: datetime, end: datetime) -> dict:
    audits = [job for job in jobs if job.get("kind") == "audit" and job.get("principal") == AUDITOR
              and within(parse_ts(job.get("created")), start, end)]
    verdicts = Counter(review.get("outcome") or review.get("verdict") for job in audits if job.get("status") == "done"
                       for review in job.get("reviews") or [] if isinstance(review, dict))
    # A first review is one of a concept no done audit reviewed before: how well the maintainer
    # writes. A re-review follows an earlier refusal, so its rate says how refusals get answered.
    done = sorted((job for job in jobs if job.get("kind") == "audit" and job.get("status") == "done"),
                  key=lambda job: (str(job.get("finished") or ""), int(job.get("finished_ns") or 0),
                                   str(job.get("id") or "")))
    seen: set[str] = set()
    first, again = Counter(), Counter()
    for job in done:
        counted = job in audits
        for review in job.get("reviews") or []:
            if not isinstance(review, dict) or not isinstance(review.get("path"), str):
                continue
            if counted:
                (again if review["path"] in seen else first)[review.get("outcome") or review.get("verdict")] += 1
            seen.add(review["path"])
    reviews = [r for r in triage if r.get("principal") == AUDITOR and r.get("status") == "done"
               and within(parse_ts(r.get("at")), start, end)]
    triage_verdicts = Counter(r.get("verdict") for r in reviews)
    return {
        "runs": sorted({job.get("run") for job in audits} | {r.get("run") for r in reviews} - {None}),
        "audit_changesets": len(audits), "audit_failed": sum(job.get("status") == "failed" for job in audits),
        "concepts": sum(verdicts.values()), "verdicts": dict(verdicts),
        "first_reviews": dict(first), "re_reviews": dict(again),
        "triage": sum(triage_verdicts.values()), "triage_verdicts": dict(triage_verdicts),
        "triage_claims": sum(len(r.get("missed") or []) for r in reviews if r.get("verdict") == "missed"),
    }


def queue_facts(items: list[dict], start: datetime, end: datetime, now: datetime) -> dict:
    ready = [item for item in items if item["status"] == "ready"]

    def since(item: dict) -> datetime | None:  # the watchdog's rule: the wait restarts on a return to ready
        stamps = [parse_ts(item.get("created_at"))]
        stamps += [parse_ts(r.get("at")) for r in item.get("reopened") or [] if isinstance(r, dict)]
        stamps += [parse_ts(h.get("at")) for h in item["attempts"].get("history") or [] if isinstance(h, dict)]
        return max(filter(None, stamps), default=None)

    waits = sorted((((now - ts).total_seconds() / 3600, item) for item in ready if (ts := since(item))),
                   key=lambda wait: wait[0])
    members = [hours for hours, item in waits if (item.get("origin") or {}).get("kind") == "member"]
    created = [item for item in items if within(parse_ts(item.get("created_at")), start, end)]
    return {
        "ready": len(ready), "ready_by_origin": dict(Counter((i.get("origin") or {}).get("kind") for i in ready)),
        "over_72h": sum(hours > 72 for hours, _i in waits), "oldest_hours": round(waits[-1][0], 1) if waits else None,
        "members_waiting": len(members), "member_oldest_hours": round(max(members), 1) if members else None,
        "needs_human": sum(item["status"] == "needs_human" for item in items),
        "parked": sum(item["status"] == "parked" for item in items),
        "intake": len(created), "intake_by_origin": dict(Counter((i.get("origin") or {}).get("kind") for i in created)),
    }


# --- grading --------------------------------------------------------------------------------


def row(area: str, name: str, grade: str, value: str, expect: str) -> dict:
    return {"area": area, "check": name, "grade": grade, "value": value, "expect": expect}


def grade(facts: dict, args: argparse.Namespace) -> list[dict]:
    m, a, q = facts["maintainer"], facts["auditor"], facts["queue"]
    rows = []
    # The maintainer: it ran, used its time, and its work passed the gate.
    if not m["runs"]:
        rows.append(row("维护", "运行", "bad", "窗口内没有任何写入", "每天 02:00 一次"))
    else:
        span = m["span_minutes"]
        # A run that took its whole item budget stopped by design, not early (maint end: "budget").
        spent = m["closed"] >= args.run_items
        busy = span >= args.min_run_minutes or q["ready"] == 0 or spent
        rows.append(row("维护", "运行时长", "ok" if busy else "bad",
                        f"{span} 分钟（{cst(parse_ts(m['first_write']))}–{cst(parse_ts(m['last_write']))}）"
                        + ("，条目额度用完" if spent and span < args.min_run_minutes else ""),
                        f"≥ {args.min_run_minutes} 分钟，或队列已清空，或用完 {args.run_items} 条额度"))
        done = m["closed"]
        rows.append(row("维护", "处理条目", "ok" if done >= args.min_items or q["ready"] == 0 else "bad",
                        f"{done}（写入 {m['outcomes'].get('curated', 0)} · 跳过 {m['outcomes'].get('skipped', 0)} · "
                        f"合并 {m['outcomes'].get('duplicate', 0)}）", f"≥ {args.min_items}，或队列已清空"))
        total = m["changesets"] + m["changesets_rejected"]
        rate = pct(m["changesets"], total)
        rows.append(row("维护", "首次过 gate", "ok" if rate is None or rate >= args.min_gate_pct else "warn",
                        f"{m['changesets']}/{total}" + (f"（{rate}%）" if rate is not None else ""),
                        f"≥ {args.min_gate_pct:g}%"))
        closed_without = m["outcomes"].get("skipped", 0) + m["outcomes"].get("duplicate", 0)
        share = pct(closed_without, done)
        rows.append(row("维护", "跳过+合并占比", "ok" if share is None or share <= args.max_skip_pct else "warn",
                        f"{closed_without}/{done}" + (f"（{share}%）" if share is not None else ""),
                        f"≤ {args.max_skip_pct:g}%（漏判风险，由 triage 抽查）"))
    # The auditor: it ran, and what it found.
    if not a["runs"]:
        rows.append(row("审计", "运行", "bad", "窗口内没有任何审计回执", "每天 07:00 一次"))
    else:
        concepts = a["concepts"]
        verified = a["verdicts"].get("verified", 0)
        rows.append(row("审计", "概念审查", "ok" if concepts >= args.min_audits else "warn",
                        f"{concepts}（通过 {verified} · 修正 {a['verdicts'].get('corrected', 0)} · "
                        f"未验证 {a['verdicts'].get('unverified', 0)}）", f"≥ {args.min_audits}，或 backlog 已清空"))
        # The maintainer's writing, judged once: a re-review follows an earlier refusal, so it is
        # shown apart (how refusals get answered) and never drags this rate down.
        # Passed = verified or corrected: a correction only narrows (deletes, weakens, attributes) and the
        # reader gets the corrected text. Verified alone sat at 9-20% 11-04..11 while verified + corrected
        # read 33-56%: a bar on verified alone asked for text the auditor never touches.
        first = a["first_reviews"]
        taken = sum(first.values())
        rate = pct(first.get("verified", 0) + first.get("corrected", 0), taken)
        rows.append(row("审计", "首审通过率", "ok" if rate is None or rate >= args.min_first_pass_pct else "warn",
                        (f"{rate}%（{taken} 个首次审查：通过 {first.get('verified', 0)} · 修正 "
                         f"{first.get('corrected', 0)} · 未验证 {first.get('unverified', 0)}）") if taken else "—",
                        f"≥ {args.min_first_pass_pct:g}%（通过 + 修正）"))
        again = a["re_reviews"]
        redone = sum(again.values())
        rows.append(row("审计", "重审通过率", "ok",
                        f"{pct(again.get('verified', 0) + again.get('corrected', 0), redone)}%（{redone} 次重审）"
                        if redone else "—", "仅参考（通过 + 修正）"))
        judged = a["triage"]
        missed = a["triage_verdicts"].get("missed", 0)
        miss = pct(missed, judged)
        rows.append(row("审计", "triage 漏判率", "ok" if miss is None or miss <= args.max_miss_pct else "warn",
                        f"{missed}/{judged}"
                        + (f"（{miss}%，{a['triage_claims']} 处遗漏）" if miss is not None else ""),
                        f"≤ {args.max_miss_pct:g}%（达标后才启动历史补扫）"))
    if facts.get("replay"):
        return rows
    if facts.get("auditor_running"):
        rows.append(row("审计", "运行中", "warn", "auditor 仍持有 lease，审计数字不完整", "质检前结束"))
    # The queue: is it draining, is anyone waiting too long, does anything need a person.
    rows.append(row("队列", "ready 积压", "ok" if q["ready"] <= args.max_ready else "warn",
                    f"{q['ready']}（新增 {q['intake']}，处理 {m['closed']}）", f"≤ {args.max_ready}"))
    rows.append(row("队列", "等待超 72h", "ok" if q["over_72h"] == 0 else "warn",
                    f"{q['over_72h']}" + (f"（最老 {q['oldest_hours']}h）" if q["oldest_hours"] else ""), "0"))
    rows.append(row("队列", "成员投递", "ok" if not q["members_waiting"] or q["member_oldest_hours"] <= 24 else "bad",
                    f"{q['members_waiting']} 条等待" + (f"，最老 {q['member_oldest_hours']}h"
                                                      if q["members_waiting"] else ""), "≤ 24h"))
    rows.append(row("队列", "needs_human", "ok" if q["needs_human"] == 0 else "bad", str(q["needs_human"]), "0"))
    status = facts.get("status")
    if status:
        triage = status.get("triage") or {}
        backlog = triage.get("backlog")
        within_bar = backlog is None or backlog <= args.max_triage_backlog
        rows.append(row("队列", "triage 积压", "ok" if within_bar else "warn",
                        f"{backlog}" if backlog is not None else "—", f"≤ {args.max_triage_backlog}"))
        audit = status.get("audit") or {}
        oldest = parse_ts(audit.get("oldest_finished"))
        hours = round((facts["now"] - oldest).total_seconds() / 3600, 1) if oldest else None
        rows.append(row("队列", "审计 backlog", "ok" if hours is None or hours <= 72 else "warn",
                        f"{audit.get('pending')} 待审" + (f"，最老 {hours}h" if hours is not None else ""),
                        "最老 ≤ 72h"))
        held = audit.get("held") or {}
        if held:
            count = held.get("count") or 0
            rows.append(row("队列", "等维护者回应", "ok" if count <= args.max_held else "warn",
                            f"{count} 个概念（连续 {held.get('after')} 次未验证，暂停重审）",
                            f"≤ {args.max_held}"))
    wiki = facts.get("wiki")
    if wiki:
        lag = wiki.get("mirror_lag_minutes")
        behind = "" if wiki["mirror_current"] else (
            f"，{wiki.get('mirror_lag_unknown') or '落后时长未知'}" if lag is None else f"，落后 {lag:g} 分钟")
        rows.append(row("读取", "镜像同步",
                        "ok" if lag is not None and lag <= args.max_mirror_lag_minutes else "bad",
                        f"镜像 {wiki['mirror_revision'] or '—'}，writer {wiki['writer_revision'] or '—'}{behind}",
                        f"镜像 = writer HEAD，或只差最近 {args.max_mirror_lag_minutes:g} 分钟的提交"))
        trust = wiki.get("by_trust") or {}
        total = sum(trust.values())
        unverified = trust.get("unverified", 0)
        share = pct(unverified, total)
        rows.append(row("读取", "未验证概念", "ok" if share is None or share <= args.max_unverified_pct else "warn",
                        f"{unverified}/{total}" + (f"（{share}%）" if share is not None else ""),
                        f"≤ {args.max_unverified_pct:g}%"))
        current = wiki.get("verified_current")
        if current is not None:
            # What a reader may rely on now: verification that confirms the current version, not an older one.
            share = pct(current, total)
            good = share is None or share >= args.min_current_pct
            rows.append(row("读取", "当前版本已验证", "ok" if good else "warn",
                            f"{current}/{total}" + (f"（{share}%）" if share is not None else ""),
                            f"≥ {args.min_current_pct:g}%"))
    return rows


def overall(rows: list[dict]) -> str:
    grades = {r["grade"] for r in rows}
    return "bad" if "bad" in grades else "warn" if "warn" in grades else "ok"


# --- the card ---------------------------------------------------------------------------------


def render_card(report: dict, label: str) -> dict:
    verdict = report["grade"]
    rows = report["rows"]
    counts = Counter(r["grade"] for r in rows)
    head = {"ok": "全部符合预期", "warn": f"{counts['warn']} 项偏离预期",
            "bad": f"{counts['bad']} 项不符合预期"}[verdict]
    if verdict == "bad" and counts["warn"]:
        head += f"，{counts['warn']} 项偏离"
    window = f"{cst(parse_ts(report['window']['start']))} → {cst(parse_ts(report['window']['end']))}（北京时间）"
    elements = [{"tag": "markdown", "content": f"**{head}** · 统计窗口 {window}"}]
    for area in dict.fromkeys(r["area"] for r in rows):
        lines = [f"{GRADES[r['grade']]} **{r['check']}**：{r['value']}"
                 + (f" <font color='grey'>（预期 {r['expect']}）</font>" if r["grade"] != "ok" else "")
                 for r in rows if r["area"] == area]
        elements += [{"tag": "hr"}, {"tag": "markdown", "content": f"**{area}**\n" + "\n".join(lines)}]
    m = report["facts"]["maintainer"]
    tail = [f"维护 run：{', '.join(r[:8] for r in m['runs']) or '无'}",
            f"审计 run：{', '.join(r[:8] for r in report['facts']['auditor']['runs']) or '无'}"]
    if report["errors"]:
        tail.append("⚠️ 部分来源读取失败：" + "；".join(e["error"][:80] for e in report["errors"]))
    elements.append({"tag": "markdown", "content": f"<font color='grey'>{' · '.join(tail)} · {label}</font>",
                     "text_size": "notation"})
    title = {"ok": "✅ AI Wiki 每日质检：符合预期", "warn": "⚠️ AI Wiki 每日质检：有偏离",
             "bad": "🔴 AI Wiki 每日质检：不符合预期"}[verdict]
    return {"schema": "2.0", "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "subtitle": {"tag": "plain_text", "content": report["bundle"]}, "template": TEMPLATE[verdict]},
            "body": {"elements": elements}}


# --- entry point --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--bundle", required=True, help="the writer's bundle directory")
    p.add_argument("--writer-url",
                   help="the writer's base URL; adds GET /maint/status (token in $AIWIKI_WATCHDOG_TOKEN)")
    p.add_argument("--mirror-url", help="the read mirror's base URL; adds GET /health (same token)")
    p.add_argument("--now", help="end of the window, RFC 3339 (default: now)")
    p.add_argument("--hours", type=float, default=24, help="window length (default 24)")
    t = p.add_argument_group("expectations")
    t.add_argument("--min-run-minutes", type=int, default=180, help="maintainer busy time (deadline 240m)")
    t.add_argument("--min-items", type=int, default=60, help="items the maintainer closes")
    t.add_argument("--run-items", type=int, default=100,
                   help="the maintainer prompt's max_items: a run that closed this many stopped on its budget")
    t.add_argument("--min-gate-pct", type=float, default=90, help="changesets accepted on first proposal")
    t.add_argument("--max-skip-pct", type=float, default=60, help="items closed as skipped or duplicate")
    t.add_argument("--min-audits", type=int, default=60, help="concepts the auditor reviews")
    t.add_argument("--min-first-pass-pct", "--min-verified-pct", dest="min_first_pass_pct", type=float, default=50,
                   help="first reviews of a concept that ended verified or corrected")
    t.add_argument("--max-miss-pct", type=float, default=20, help="triage reviews that found missed knowledge")
    t.add_argument("--max-ready", type=int, default=150, help="items ready at the end of the window")
    t.add_argument("--max-triage-backlog", type=int, default=60, help="skips and duplicates waiting for triage")
    t.add_argument("--max-unverified-pct", type=float, default=20, help="published concepts unverified")
    t.add_argument("--min-current-pct", type=float, default=70,
                   help="published concepts whose current version is verified")
    t.add_argument("--max-held", type=int, default=60, help="concepts the audit holds for the maintainer's answer")
    t.add_argument("--max-mirror-lag-minutes", type=float, default=15,
                   help="age of the oldest writer commit the mirror lacks (it pulls every 5 minutes)")
    p.add_argument("--label", default=socket.gethostname(), help="shown in the card's footer")
    return p


def report(args: argparse.Namespace) -> dict:
    now = parse_ts(args.now) if args.now else datetime.now(UTC)
    if now is None:
        raise SystemExit("--now must be an RFC 3339 timestamp")
    start = now - timedelta(hours=args.hours)
    bundle = Path(args.bundle).expanduser().resolve()
    errors: list[dict] = []
    items, jobs = load_items(bundle), load_jobs(bundle)
    facts: dict = {"now": now, "maintainer": maintainer_facts(items, jobs, start, now),
                   "auditor": auditor_facts(jobs, load_triage(bundle), start, now),
                   "queue": queue_facts(items, start, now, now)}
    token = os.environ.get("AIWIKI_WATCHDOG_TOKEN")
    facts["replay"] = bool(args.now)
    lease = read_json(bundle / ".okf" / "maint" / "lease-auditor.json")
    until = parse_ts((lease or {}).get("expires_at"))
    facts["auditor_running"] = not args.now and until is not None and now < until
    if args.writer_url and not args.now:
        try:
            facts["status"] = writer_get(args.writer_url, token or "", "/maint/status", bundle.name)
        except SourceError as exc:
            errors.append({"source": "writer", "error": str(exc)})
    if args.mirror_url and not args.now:
        try:
            health = writer_get(args.mirror_url, token or "", "/health", bundle.name)
            head = git_head(bundle)
            served = health.get("git_revision") or None
            lag, why = mirror_lag(bundle, served, head, now)
            facts["wiki"] = {"mirror_revision": (served or "")[:12] or None,
                             "writer_revision": head and head[:12], "by_trust": health.get("by_trust"),
                             "concepts": health.get("concepts"), "verified_current": health.get("verified_current"),
                             "mirror_current": bool(head) and served == head,
                             "mirror_lag_minutes": lag, "mirror_lag_unknown": why or None}
        except SourceError as exc:
            errors.append({"source": "mirror", "error": str(exc)})
    rows = grade(facts, args)
    facts["now"] = iso(now)
    facts["maintainer"]["parked"] = dict(facts["maintainer"]["parked"])
    facts.pop("status", None)
    return {"schema": "ai-wiki.daily-qc/v1", "bundle": bundle.name, "window": {"start": iso(start), "end": iso(now)},
            "grade": overall(rows), "rows": rows, "facts": facts, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):  # noqa: UP036 - installed standalone beside the watchdog
        print(json.dumps({"errors": [{"source": "python", "error": "Python 3.11+ required"}]}))
        return 2
    args = build_parser().parse_args(argv)
    try:
        result = report(args)
    except SourceError as exc:
        print(json.dumps({"grade": None, "errors": [{"source": "bundle", "error": str(exc)}]}, ensure_ascii=False))
        return 2
    result["card"] = render_card(result, args.label)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
