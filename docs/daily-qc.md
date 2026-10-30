# Daily quality check

`scripts/daily_qc.py` grades one day of maintenance against what the owner expects. The
watchdog pages when maintenance stops; this report says whether the day's work was good.
It uses only the standard library, only reads, and runs no LLM.

It runs on the writer host at 10:30 CST, after the 02:00 maintainer (up to 4 h) and the
07:00 auditor. The window is the 24 hours before the run.

## Rows

| Area | Row | Source | Expectation (flag, default) |
|---|---|---|---|
| 维护 | 运行时长 | First to last maintainer write that day: item closes and parks, changeset jobs | `--min-run-minutes` 180, or the queue is empty |
| 维护 | 处理条目 | Items the maintainer closed (curated, skipped, duplicate…), including closes a triage review reopened since | `--min-items` 60, or the queue is empty |
| 维护 | 首次过 gate | The maintainer's changeset jobs, done vs rejected | `--min-gate-pct` 90 |
| 维护 | 跳过+合并占比 | Closed as skipped or duplicate | `--max-skip-pct` 60 |
| 审计 | 概念审查, 通过率 | The auditor's audit jobs and their review outcomes | `--min-audits` 60, `--min-verified-pct` 50 |
| 审计 | triage 漏判率 | `.okf/triage` receipts: missed vs upheld | `--max-miss-pct` 20; the historical backfill waits for this |
| 队列 | ready 积压, 等待超 72h | Items ready now; the wait restarts on a return to ready, as the watchdog counts it | `--max-ready` 150; none over 72 h |
| 队列 | 成员投递, needs_human | Member items waiting; items needing a person | ≤ 24 h; 0 (red) |
| 队列 | triage 积压, 审计 backlog | `GET /maint/status` (`--writer-url`) | `--max-triage-backlog` 60; oldest ≤ 72 h |
| 读取 | 镜像同步, 未验证概念 | The mirror's `GET /health` (`--mirror-url`) against the writer's Git HEAD | Same revision (red otherwise); `--max-unverified-pct` 20 |

A row is ✅ ok, ⚠️ warn or 🔴 bad. The card is green, yellow or red by its worst row.
`运行` turns red when a role left no receipt that day; `运行中` warns when the auditor
still holds its lease at report time.

`--now <RFC 3339>` replays a past window from the receipts' timestamps (the work rows only:
the queue and wiki rows describe the present) and never posts.

Exit `0`: the report was made, whatever its grade. Exit `2`: a source could not be read,
or the Feishu post failed. The JSON then lists them under `errors`, and the card notes them.

## Install (writer host)

It reuses the watchdog's mode-600 `/etc/ai-wiki-watchdog.env`, which holds
`AIWIKI_WATCHDOG_FEISHU_WEBHOOK` and the read token `AIWIKI_WATCHDOG_TOKEN`. The deploy
copies `scripts/` into `/home/admin/app`.

```ini
# /etc/systemd/system/ai-wiki-daily-qc.service
[Unit]
Description=AI Wiki daily quality check
After=network-online.target

[Service]
Type=oneshot
User=admin
EnvironmentFile=/etc/ai-wiki-watchdog.env
ExecStart=/home/admin/app/.venv/bin/python /home/admin/app/scripts/daily_qc.py --bundle /home/admin/solvely-wiki --writer-url http://127.0.0.1:8788 --mirror-url http://127.0.0.1:8787 --label bwg-la-writer
TimeoutStartSec=300
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only

# /etc/systemd/system/ai-wiki-daily-qc.timer
[Timer]
OnCalendar=*-*-* 02:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

Dry run without posting: `… daily_qc.py --bundle … --no-post`.
