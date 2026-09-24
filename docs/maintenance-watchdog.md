# Maintenance watchdog

`scripts/maintenance_watchdog.py` is a small script for watching the daily maintenance.
It uses only the standard library, only reads, and runs no LLM. It never fixes anything.
It needs Python 3.11 or later; on an older `python3` it exits `2` with
`Python 3.11+ required`.
It reports when maintenance has stopped making progress, and can post the report to a
Feishu group. It exists because the checkpoint once stayed frozen for 9 days with nobody
noticing (08-18..08-26), and several autopilot issues were left stuck for days
(WAIO-499/512/545).

## Checks

You turn on each group of checks with a flag. Every threshold can be changed with a flag.

| Flag | Source | Alerts when (default) |
|---|---|---|
| `--multica` | `multica` CLI, autopilot `5c80732b-…` (`--autopilot-id`) | The newest `ai_wiki_incremental_checkpoint_v4.completed_at` found in any run issue's metadata is older than `--checkpoint-max-age-hours` (30). The latest run failed and no checkpoint was written after that run started. The latest run is older than `--run-max-age-hours` (26), which means the schedule did not fire. A run issue has been in `todo`/`in_progress` longer than `--stuck-hours` (3). A run has not reached a terminal state after `--stuck-hours`, and its issue is not done or cancelled. |
| `--ledger PATH` | `ai-wiki maintain` `state.json`, or its state directory | An entry is `needs_repair`. An entry has been pending longer than `--pending-max-age-hours` (48); the pending time is measured from the earlier of its frozen evidence mtime and its first job. `done` and `superseded` entries are ignored. |
| `--bundle PATH` (repeatable) | The writer's bundle directory | The bundle's last Git commit is older than `--commit-max-age-hours` (48). A job in `<bundle>/.okf/jobs/` failed within the last `--unresolved-failure-hours` (168, 7 days) and no later attempt on the same source SHA (ingest) or parent job (audit) is done, queued or running. A queued or running job is older than `--stuck-hours`. The output lists every failure from the last `--failed-window-hours` (24) with the attempt that resolved it, and reports queue depth and the age of the oldest queued job. |
| `--bundle PATH` (same flag) | The maintainer queue in `<bundle>/.okf/maint/` (`service/maint_state.py`, design §7) | An item is `needs_human` (one alert per item, so each new one posts at once). Some item has waited in `ready` longer than `--ready-max-age-hours` (72), counted from its creation or from its last admin retry; one alert per bundle names the count and the oldest item. The `repos` or `issues` cursor has not advanced for `--cursor-max-age-hours` (30); a cursor that does not exist yet never alerts, so there is nothing before Phase 2 starts. A maintainer or auditor run lease has been held longer than `--stuck-hours`, or has lapsed that long ago without `maint end` and without a new run taking it over. A missing `.okf/maint` is quiet, not an error. |

The script prints one JSON document containing `status`, `alerts[]`, `errors[]`, per-check
`checks` facts, and `notify`. Exit codes:

- `0`: ok.
- `1`: alert.
- `2`: error. A source could not be read, the Feishu post failed, the state file could not
  be written, the flags were wrong, or the watchdog itself crashed. An unexpected exception
  never exits `1`; its traceback goes to stderr with the webhook and secret redacted.

A check that cannot read its source counts as an error, never as healthy. Errors are also
included in the alert fingerprint, so a watchdog that has lost access to its sources still
sends a message.

## Feishu notification and dedup

Add a custom bot to the target group. Signature verification is recommended; if you
use it, also pass the signing secret. You can instead set a keyword check with the keyword
`AI Wiki`. Keep the URL and secret out of unit files and shell history by putting them in a
mode-600 environment file:

```bash
# /etc/ai-wiki-watchdog.env   (root:root, chmod 600)
AIWIKI_WATCHDOG_FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/<id>
AIWIKI_WATCHDOG_FEISHU_SECRET=<signing secret, omit if the bot has no signature check>
```

The script reads these as the defaults for `--feishu-webhook` and `--feishu-secret`. A
webhook requires `--state-file`. The alert fingerprint is a hash of the sorted alert keys.
The keys identify things, never ages, for example
`checkpoint_stale:<completed_at>`, `issue_stuck:<issue>:<status>` or
`job_failed:<bundle>:<job>`. A message goes out only when the fingerprint changes, and
one recovery message goes out when every alert clears. If a post fails, the run exits `2`
and leaves the state file unchanged, so the next run tries again. If the state file cannot
be written after a post, the run also exits `2`, and the same message repeats on each run
until the file is writable again. Example:

```text
【AI Wiki 维护告警】multica-runtime
1. checkpoint 已 80.5h 未推进（WAIO-547 completed_at 2026-09-20T10:35:37Z，阈值 30h）
2. WAIO-499 停在 in_progress 已 191.0h（阈值 3h）
3. 最新 autopilot run（WAIO-587，2026-09-22T20:00:31Z）失败且之后无新 checkpoint：issue blocked
检查时间 2026-09-23T19:03:07Z
```

## Writer host (aliyun-jp)

The writer checks read the worker's local files, so they must run on the writer host. Run
them as the worker's user (`admin`). Git then owns the repository it reads, so there is no
`safe.directory` warning, and the job files are readable.

1. Find the bundle path in the worker unit:

   ```bash
   systemctl cat ai-wiki-worker.service | grep -E '^(User|WorkingDirectory|EnvironmentFile)='
   sudo grep -hE '^AIWIKI_BUNDLES?=' <EnvironmentFile listed above>
   ```

   - Multi-bundle mode (`AIWIKI_BUNDLES=<root>`): the bundle is `<root>/<name>`, for
     example `/home/admin/bundles-rw/solvely-wiki`.
   - Single-bundle mode: the bundle is the `AIWIKI_BUNDLE` directory itself.

   Either way, the jobs live in `<bundle>/.okf/jobs/<id>.json`. That directory is ignored
   by Git and exists only on the writer, next to the bundle's `.git`. Check it:

   ```bash
   sudo -u admin ls /home/admin/bundles-rw/solvely-wiki/.okf/jobs | tail -3
   ```

2. Install the script from a checkout at the deployed revision. Check the Python version
   first. If the system `python3` is older than 3.11, start the script with a 3.11+
   interpreter in `ExecStart`, for example `/usr/bin/python3.11 /usr/local/bin/ai-wiki-watchdog …`.

   ```bash
   python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
   sudo install -m 0755 scripts/maintenance_watchdog.py /usr/local/bin/ai-wiki-watchdog
   sudo -u admin /usr/local/bin/ai-wiki-watchdog \
     --bundle /home/admin/bundles-rw/solvely-wiki --label aliyun-jp-writer; echo "exit=$?"
   ```

   This dry run prints JSON only. It sends nothing and persists nothing.

3. Create the units:

   ```ini
   # /etc/systemd/system/ai-wiki-watchdog.service
   [Unit]
   Description=AI Wiki maintenance watchdog (writer checks)
   Wants=network-online.target
   After=network-online.target

   [Service]
   Type=oneshot
   User=admin
   Environment=HOME=/home/admin
   EnvironmentFile=/etc/ai-wiki-watchdog.env
   StateDirectory=ai-wiki-watchdog
   ExecStart=/usr/local/bin/ai-wiki-watchdog \
     --bundle /home/admin/bundles-rw/solvely-wiki \
     --state-file /var/lib/ai-wiki-watchdog/writer.json \
     --label aliyun-jp-writer
   # exit 1 = alert (already delivered); 2 = watchdog error or crash marks the unit failed
   SuccessExitStatus=1
   TimeoutStartSec=300
   NoNewPrivileges=yes
   ProtectSystem=strict
   ProtectHome=read-only
   ```

   ```ini
   # /etc/systemd/system/ai-wiki-watchdog.timer
   [Unit]
   Description=Hourly AI Wiki maintenance watchdog

   [Timer]
   OnCalendar=*-*-* *:07:00
   RandomizedDelaySec=60
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now ai-wiki-watchdog.timer
   systemctl list-timers 'ai-wiki-watchdog*'
   journalctl -u ai-wiki-watchdog.service -n 50 --no-pager
   ```

   The timer runs every hour, but dedup means each change produces at most one message.
   Hourly runs catch the 3-hour stuck thresholds on the same morning. For example, the
   WAIO-545 run crossed 3 hours at 23:00:23Z on 09-18, and the 00:07Z run would have
   reported it. `systemctl --failed` shows only real watchdog errors.

   The watchdog only reads, so writer deploys and restarts do not need to wait for it.

## Multica runtime host (optional)

The Multica and ledger checks need the `multica` CLI to be logged in to the workspace with
read access, and they need the maintainer's durable ledger. Both are on the agent runtime
host (currently `ip-10-2-192-225`, user `ubuntu`). The autopilot uses
`${XDG_STATE_HOME:-$HOME/.local/state}/ai-wiki-maintainer/solvely-wiki` as its ledger
directory.

```bash
multica autopilot runs 5c80732b-67a6-4e33-ba22-c620a94e27c1 --limit 1 --output json   # auth smoke test
/usr/local/bin/ai-wiki-watchdog --multica \
  --ledger ~/.local/state/ai-wiki-maintainer/solvely-wiki --label multica-runtime; echo "exit=$?"
```

Use the same unit and timer as on the writer host, with these changes:

```ini
User=ubuntu
Environment=HOME=/home/ubuntu
ExecStart=/usr/local/bin/ai-wiki-watchdog --multica \
  --multica-bin /home/ubuntu/.local/bin/multica \
  --ledger /home/ubuntu/.local/state/ai-wiki-maintainer/solvely-wiki \
  --state-file /var/lib/ai-wiki-watchdog/runtime.json \
  --label multica-runtime
```

- `HOME` must be set explicitly. The `multica` CLI reads its login from the home directory.
  This is the same failure the bundle pull unit had when it ran without `HOME`.
- Point `--multica-bin` at `command -v multica`. It accepts a command prefix such as
  `'multica --profile prod'`.
- Run `--multica` on exactly one host so that each Multica alert is posted only once. The
  writer host can run it instead, if `multica` is installed and logged in there.

The script only calls `autopilot runs`, `autopilot get`, `issue list`, `issue get` and
`issue timeline`. Each run makes three listing calls, plus one timeline call for each issue
that is currently in `todo`/`in_progress`. `issue get` is used only for run issues that have
been reassigned away from the agent. On 2026-09-23 a run made 6 calls and finished in about
7 seconds.

## Clearing alerts

- **Orphaned issues** (`issue_stuck`): the alert stays until someone moves the issue out
  of `todo`/`in_progress`. Closing or cancelling the issue also clears `run_stuck` for its
  run. This matters because the `multica` CLI has no command to change a run's status, and
  the WAIO-545 run has been in `issue_created` since 09-18.
- **`latest_run_failed`**: clears when a checkpoint is written after that run started, for
  example after a manual recovery on the same issue (WAIO-547, WAIO-427), or when the next
  scheduled run succeeds.
- **`ledger_needs_repair`**: clears once the entry is resolved through `ai-wiki maintain`.
- **`maint_needs_human`**: clears when the owner reopens the item (`POST /admin/items/<id>/retry`) or
  closes it (`POST /admin/items/<id>/resolve`), or when a new build re-admits an attempt-capped item at
  the next `maint begin`. Run `ai-wiki maint status` first to see the reason.
- **`maint_ready_stale`**: maintainer runs are not draining the queue (not scheduled, failing,
  or too few items per run). It clears once no ready item is older than the threshold.
- **`maint_cursor_stale`**: no `maint collect` has succeeded for that collector. It clears on
  the next successful collect, which rewrites the cursor even when nothing changed.
- **`maint_lease_stuck`**: `held` means a run is still renewing its lease past `--stuck-hours`;
  check that run's issue. `expired` means a run died without `maint end`; the next `maint begin`
  takes the lease over and returns the run's item to `ready`, which clears it.
- **`job_failed`**: clears once a later attempt on the same source or parent job is queued,
  running or done. A failure that nobody retries keeps alerting for
  `--unresolved-failure-hours` (7 days), well past the 24-hour listing window and the
  ledger's 48-hour `ledger_pending_stale` alert for the same source. After that the writer
  stops reporting it. If nothing else is alerting, that sends a recovery message, so retry the
  source within the week or record why it is abandoned. A `maintain` ledger entry cannot be
  dropped: a `needs_repair` one keeps its `ledger_needs_repair` alert until it recovers.

## Historical replay

`--now <RFC3339>` evaluates the Multica checks at a past instant, and never notifies:

- Runs created after that instant are ignored.
- Runs that completed after it count as still open.
- Checkpoints written after it are ignored.
- Issue status is rebuilt from the `status_changed` timeline.

Pass the v3 key as well for dates before v4 existed. Ledger, writer and maint checks read the
files as they are today and only measure ages from `--now`.

```bash
ai-wiki-watchdog --multica --runs-limit 100 \
  --checkpoint-key ai_wiki_incremental_checkpoint_v4 \
  --checkpoint-key ai_wiki_incremental_checkpoint_v3 \
  --now 2026-09-18T23:00:00Z
```

The table below comes from a read-only replay against production on 2026-09-23. Times are
07:00 and 08:00 Asia/Shanghai.

| Morning | `--now` (UTC) | Result | Alerts |
|---|---|---|---|
| 08-19 | 08-18T23:00Z, 08-19T00:00Z | alert | Latest run WAIO-146 failed. The checkpoint was 25.8h old, so this was not yet a stale-checkpoint alert. |
| 09-16 | 09-15T23:00Z | alert | Latest run WAIO-499 failed with "Selected model is at capacity". |
| 09-16 | 09-16T00:00Z | alert | The same, plus WAIO-499 in `in_progress` for 4.0h. |
| 09-19 | 09-18T23:00Z | alert | WAIO-499 in `in_progress` for 75h. WAIO-512 in `todo` for 51h. |
| 09-19 | 09-19T00:00Z | alert | The same, plus WAIO-545 in `todo` and its run in `issue_created`, both for 4h. |
| 09-21 | 09-20T23:00Z, 09-21T00:00Z | alert | Latest run WAIO-559 failed, plus the three orphaned issues and the WAIO-545 run. |
| 08-18, 08-29, 09-13, 09-14, 09-15 | 23:00Z the day before | ok | None. |
| 08-22 | 08-22T00:00Z | alert | Checkpoint (WAIO-143) 98.8h old. Latest run WAIO-162 failed. |
| 09-20 12:00Z, 09-10 13:00Z | | no run alert | The failed runs WAIO-547 and WAIO-427 were followed by a checkpoint on the same issue. |

`tests/test_maintenance_watchdog.py` replays the same production snapshot, with the data
reduced to lifecycle fields, through a fake `multica` CLI. It also replays the real writer
job receipts.
