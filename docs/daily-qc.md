# Daily quality check

`scripts/daily_qc.py` grades one day of maintenance against what the owner expects. The
watchdog pages when maintenance stops; this report says whether the day's work was good.
It uses only the standard library, only reads, and runs no LLM.

It runs on the writer host at 10:30 CST, after the 02:00 maintainer (up to 4 h) and the
07:00 auditor. The window is the 24 hours before the run. Its JSON carries the Feishu card
(`card`); delivery is the caller's, so no chat credential lives on the writer.

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
the queue and wiki rows describe the present).

Exit `0`: the report was made, whatever its grade. Exit `2`: a source could not be read.
The JSON then lists it under `errors`, and the card notes it.

## Install

The deploy copies `scripts/` into `/home/admin/app` on the writer host. The script reads the
watchdog's read token `AIWIKI_WATCHDOG_TOKEN` from its mode-600 `/etc/ai-wiki-watchdog.env`:

```bash
set -a; . /etc/ai-wiki-watchdog.env; set +a
sudo -u admin env AIWIKI_WATCHDOG_TOKEN="$AIWIKI_WATCHDOG_TOKEN" /home/admin/app/.venv/bin/python \
  /home/admin/app/scripts/daily_qc.py --bundle /home/admin/solvely-wiki \
  --writer-url http://127.0.0.1:8788 --mirror-url http://127.0.0.1:8787 --label bwg-la-writer
```

The owner's Hermes runs exactly that over ssh every day at 10:30 CST (a no-agent cron job,
`~/.hermes/scripts/ai_wiki_daily_qc.py`) and sends `card` to the owner's private chat with
its own Feishu bot, not to the team group the watchdog pages.
