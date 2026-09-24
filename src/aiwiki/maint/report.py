"""The deterministic maintenance run report (design §4.9); no LLM and no I/O.

``build`` turns what a run leaves behind into one JSON report: the run's local record
(budget, taken items, collection results, resubmitted audits), what ``propose`` sent, and
the writer's receipts (items, changeset jobs, cursors, the ``/maint/status`` snapshot).
``render`` prints it as the Markdown comment the run posts. The same inputs always give
the same bytes: every list is sorted and nothing reads a clock.

``issue_status`` is ``blocked`` when the run never collected or a collector failed or was
unavailable, else ``done``; parked and needs_human items raise their own alerts and never
block the run's issue. The report's ``cursors`` are the writer's cursor records, so
``ai-wiki admin cursor import <report.json>`` restores them after a writer disk loss.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from aiwiki.service.maint_state import MAX_COUNTED

SCHEMA = "ai-wiki.maint-report/v1"
BLOCKING = ("failed", "unavailable")


def _item(item: dict) -> dict:
    resolution = item.get("resolution") or {}
    history = item["attempts"]["history"]
    status, detail = item["status"], None
    if status == "curated":
        detail = f"changeset {resolution.get('job')}"
    elif status in ("skipped", "duplicate"):
        detail = resolution.get("reason")
    elif status == "split":
        detail = "into " + ", ".join(resolution.get("children") or [])
    elif status == "needs_human":
        detail = f"{resolution.get('reason')} ({resolution.get('class')})"
    elif status in ("parked", "ready") and history:
        detail = f"{history[-1].get('class')} {item['attempts']['counted']}/{MAX_COUNTED}"
    return {"id": item["id"], "topic_key": item["topic_key"], "status": status, "detail": detail,
            "attempts": {key: item["attempts"][key] for key in ("started", "counted")}}


def _gate(proposals: list[dict]) -> dict:
    """First-submission pass rate per item, and the error codes of every refused submission."""
    sent = [row for row in proposals if row.get("sent")]
    first: dict[str, dict] = {}
    for row in sent:
        for item in row.get("items") or ["(upload)"]:
            first.setdefault(item, row)
    errors = Counter(code for row in proposals if row.get("exit") for code in row.get("errors") or [])
    return {"submitted": len(sent), "items": len(first),
            "first_pass": sum(row.get("exit") == 0 for row in first.values()),
            "errors": dict(sorted(errors.items(), key=lambda pair: (-pair[1], pair[0])))}


def _changeset(job: dict) -> dict:
    return {"id": job.get("id"), "status": job.get("status"), "commit": job.get("commit"),
            "noop": bool(job.get("noop")), "work_items": job.get("work_items") or [],
            "concept_files": job.get("concept_files") or [], "deprecated_files": job.get("deprecated_files") or []}


def _cursor(record: dict | None) -> dict | None:
    if not record:
        return None
    value = record.get("value") or {}
    if "updated_at" in value and "id" in value:  # issues: (updated_at, id)
        position = {"updated_at": value.get("updated_at"), "id": value.get("id")}
    else:  # repos: {<remote>: {branch, sha, stale_since, error}}
        position = {"repos": len(value), "stale": sorted(key for key, row in value.items()
                                                         if isinstance(row, dict) and row.get("stale_since"))}
    return {"updated_at": record.get("updated_at"), **position}


def _hours(since: str | None, until: str) -> int | None:
    try:
        return int((datetime.fromisoformat(until.replace("Z", "+00:00"))
                    - datetime.fromisoformat(str(since).replace("Z", "+00:00"))).total_seconds() // 3600)
    except (TypeError, ValueError):
        return None


def build(run: dict, *, items: list[dict], proposals: list[dict], jobs: list[dict], cursors: dict,
          status: dict, released: dict, ended_at: str) -> dict:
    """The run's report as JSON; ``render`` prints it."""
    collect = run.get("collect") or {}
    blocked = sorted(name for name, result in collect.items() if result.get("status") in BLOCKING)
    rows = sorted((_item(item) for item in items), key=lambda row: (row["topic_key"], row["id"]))
    states = Counter(row["status"] for row in rows)
    counts = status.get("items") or {}
    started = run.get("started_at") or ""
    since = run.get("acquired_at") or started  # the writer's clock, as needs_human ``since`` is
    new_needs_human = sorted((entry for entry in status.get("needs_human") or []
                              if str(entry.get("since") or "") >= since),
                             key=lambda entry: (entry.get("topic_key") or "", entry.get("id") or ""))
    audit = status.get("audit") or {}
    return {
        "schema": SCHEMA,
        "run": run.get("run"),
        "bundle": run.get("bundle"),
        "issue_status": "blocked" if blocked or not collect else "done",
        "blocked_by": blocked if collect else ["collect"],
        "started_at": started,
        "ended_at": ended_at,
        "collect": collect,  # in collector order, as the run recorded it
        "cursors": {name: record for name, record in cursors.items() if record},
        "cursor_moves": {name: {"before": _cursor((run.get("cursors_before") or {}).get(name)),
                                "after": _cursor(record)} for name, record in cursors.items()},
        "queue": {
            "taken": len(rows), **{state: states[state] for state in (
                "curated", "skipped", "duplicate", "parked", "split", "needs_human", "in_progress")},
            "returned": states["ready"],  # released mid-item, back in the queue
            "remaining": sum((counts.get(state) or {}).get("count", 0) for state in ("ready", "parked")),
            "stopped": run.get("stopped") or "ended",
            "budget": {"max_items": run.get("max_items"), "deadline": run.get("deadline")},
        },
        "items": rows,
        "changesets": [_changeset(job) for job in sorted(jobs, key=lambda job: (str(job.get("created")),
                                                                                 str(job.get("id"))))],
        "gate": _gate(proposals),
        "audit": {"mode": audit.get("mode"), "pending": audit.get("pending"), "queued": audit.get("queued"),
                  "oldest_hours": _hours(audit.get("oldest_finished") or audit.get("oldest_queued"), ended_at),
                  **{key: (run.get("audits") or {}).get(key) or [] for key in ("resubmitted", "needs_human")},
                  **({"error": run["audits"]["error"]} if (run.get("audits") or {}).get("error") else {})},
        "needs_human": [{"id": entry.get("id"), "topic_key": entry.get("topic_key"),
                         "reason": (entry.get("resolution") or {}).get("reason")} for entry in new_needs_human],
        "lease": released,
    }


def _collector(name: str, result: dict) -> str:
    if result.get("status") in BLOCKING:
        return f"{name} {result['status']}: {result.get('error')}"
    line = f"{name} {result.get('status')}"
    if name == "repos":
        line += (f" {result.get('scanned', 0)} scanned (changed {result.get('changed', 0)}, "
                 f"new {result.get('new', 0)}, rebaselined {result.get('rebaselined', 0)}, "
                 f"failed {result.get('failed', 0)})")
    else:
        line += f" {result.get('changed', 0)} changed"
    return line + f", {result.get('candidates', 0)} items"


def _position(cursor: dict | None) -> str:
    if cursor is None:
        return "none"
    if "repos" in cursor:
        stale = f", {len(cursor['stale'])} stale" if cursor["stale"] else ""
        return f"{cursor['updated_at']} ({cursor['repos']} repos{stale})"
    return f"({cursor['updated_at']}, {cursor['id']})"


def render(report: dict) -> str:
    """The report as the Markdown comment of the run's issue."""
    queue, gate, audit = report["queue"], report["gate"], report["audit"]
    enqueued = Counter()
    for result in report["collect"].values():
        enqueued.update({key: result.get(key, 0) for key in ("created", "merged", "duplicate")})
    parked = ", ".join(f"{cls} {count}" for cls, count in sorted(Counter(
        row["detail"].split()[0] for row in report["items"] if row["status"] == "parked").items()))
    lines = [
        f"AI Wiki maintenance {report['run']} ({report['started_at']}) status={report['issue_status']}",
        "collect: " + (" | ".join(_collector(name, result) for name, result in report["collect"].items()) or "none")
        + f" | enqueued {enqueued['created']} (merged {enqueued['merged']}, duplicate {enqueued['duplicate']})",
        "cursors: " + (" | ".join(f"{name} {_position(move['before'])} -> {_position(move['after'])}"
                                  for name, move in report["cursor_moves"].items()) or "none"),
        f"queue: taken {queue['taken']} | curated {queue['curated']} | skipped {queue['skipped']}"
        f" | duplicate {queue['duplicate']} | parked {queue['parked']}" + (f" ({parked})" if parked else "")
        + f" | split {queue['split']} | returned {queue['returned']} | remaining {queue['remaining']}"
        f" ({queue['stopped']})",
        "changesets: " + ("; ".join(
            f"{job['id']} {(job['commit'] or 'noop')[:12]} " + " ".join(job["concept_files"] + [
                f"{path}(deprecated)" for path in job["deprecated_files"]]) for job in report["changesets"]) or "none"),
        f"gate: first submission passed {gate['first_pass']}/{gate['items']}"
        + "".join(f"; {code} x{count}" for code, count in gate["errors"].items()),
        f"audit backlog: {audit['pending']} pending, {audit['queued']} queued"
        + (f", oldest {audit['oldest_hours']}h" if audit["oldest_hours"] is not None else "")
        + f" ({audit['mode']}); resubmitted {len(audit['resubmitted'])}"
        + (f"; over the retry cap: {', '.join(audit['needs_human'])}" if audit["needs_human"] else ""),
        "needs human: " + (", ".join(f"{row['id']} {row['topic_key']} ({row['reason']})"
                                     for row in report["needs_human"]) or "none"),
        "",
        "items:",
        *(f"- {row['id']} {row['topic_key']}: {row['status']}" + (f" ({row['detail']})" if row["detail"] else "")
          for row in report["items"]),
        "",
        "cursor JSON (off-site copy for `ai-wiki admin cursor import`):",
        "```json",
        json.dumps({"cursors": report["cursors"]}, ensure_ascii=False, sort_keys=True),
        "```",
    ]
    if not report["items"]:
        lines[lines.index("items:")] = "items: none"
    return "\n".join(lines) + "\n"
