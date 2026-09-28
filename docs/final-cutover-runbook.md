# Final cut-over runbook: the server runs no LLM

This runbook takes production from today's legacy path (ingest, Codex curation on the
writer, Codex audit on the writer, a ledger checkpoint) straight to the final state: every
model-driven step runs in an external agent that holds a credential, and the writer is only
the deterministic gate. It needs no Codex, no 9Router and no model on the server, and any
agent with a principal of the right role can maintain, audit or submit
([docs/external-agents.md](external-agents.md)).

The final state is these writer flags, and nothing else changes on the server:

| Flag | Final value | Today |
|---|---|---|
| `AIWIKI_CHANGESETS_COMMIT` | `solvely-wiki,solvely-wiki-shadow` | `solvely-wiki-shadow` |
| `AIWIKI_INTAKE` | `inbox`: a member's submission becomes a work item | `curate` (Codex) |
| `AIWIKI_AUDIT` | `external`: the Auditor agent reviews the server-derived backlog | `codex` |
| `AIWIKI_LLM` | `off`: the writer never starts an agent process, ignores `config.agent` | `codex` |
| `AIWIKI_BACKLOG_EPOCH` | the moment of step 6 | unset |
| `AIWIKI_CODEX_AUDIT_MANUAL` | empty | `solvely-wiki-shadow` |

Every step below has its verification and its rollback; §13 rolls back to the legacy path
from any point. Until step 11 removes the Codex configuration from the writer host, rolling
back is removing drop-ins and restoring Multica settings from the archive step 1 makes.

Requires the merged build of the three final-state units: W14 (inbox intake,
`/admin/inbox/requeue`), W16/W17 (audit changesets, `/audit/backlog`,
`AIWIKI_BACKLOG_EPOCH`, the `review` verbs, `skills/ai-wiki-auditor`,
`docs/prompts/auditor-*.md`) and this package (`AIWIKI_LLM`, the production prompts,
this runbook). Step 0's checks refuse a build without them.

## Where and when

- **laptop**: the owner's machine, with a checkout of the merged build, `multica` logged in,
  `jq`, and the owner token in the password manager. Laptop files go to `$FL`.
- **host**: aliyun-jp as root in bash (`ssh aliyun-jp`), with the §1 helpers defined.
  Backups and header files go to `$FS` (root, 0700).
- **runtime**: ip-10-2-192-225, runtime `df0fb673`, reachable only through one-time Multica
  issues. Each such issue below is assigned to the production agent `1dcccd34` (its custom
  env holds the `process:ai-wiki-maintainer` token those steps need) and its exact text is
  given. Create it from `$FL` on the laptop:
  `multica issue create --title "<title>" --assignee-id $PROD_AGENT --description-file <file>`,
  then read the agent's comment with `multica issue comment list <issue-id> --output json`.
- **auditor host**: the host of the Auditor agent's runtime. Recommended: `Codex
  (macminim4.local)`, runtime `1ae1dfab`, which the owner manages: another host and another
  model family than the maintainer (§5). The Auditor must never share the maintainer's OS
  user (docs/external-agents.md §1).

Day −1 (owner online): steps 0 to 2 at any time, then 3a after the shadow's 13:30 CST run;
none of them changes production's behaviour. Day 0: 3b right after the 04:00 CST legacy run
finishes, then 4 to 7 in one sitting (about three hours, step 6 not before 06:30, all before
12:00 CST). Step 8 on day 0 evening or day 1, after two clean
scheduled runs; step 9 with step 8; step 10 on day 0; step 11 after 14 clean days. Never
restart the writer between 03:30 and 06:30 CST, nor while a job or lease is live
(`restart_idle` refuses those).

## Plan

| # | Step | Where | Changes production | Rollback |
|---|---|---|---|---|
| 0 | Merge, deploy procedure, deploy the build | laptop, host | no: every new flag defaults to today | redeploy `9d62536` |
| 1 | Pre-flight, archive, runtime CLI (issue R1), member notice | all | no | reinstall `PREV_CLI` |
| 2 | Auditor: principal, skill, agent, autopilot without schedule, preflight (issue A1) | host, laptop, auditor host | no | §2 |
| 3 | 3a retire the shadow agent (day −1); 3b stop the legacy run (day 0) | laptop, host | legacy schedule paused | §3 |
| 4 | Import the cursors and the ledger (issue R2) | runtime | writer queue seeded | §4 |
| 5 | Switch the production agent: skills, instructions, prompt, schedule (still paused) | laptop | agent config | §5 |
| 6 | Writer drop-in: the final flags except `AIWIKI_LLM` | host | yes | §6 |
| 7 | Canary: 1 item, one auditor run, then full schedules | laptop, host | yes | §13 |
| 8 | The writer's Codex off: `AIWIKI_LLM=off` | host | guarantee only | §8 |
| 9 | Members: the legacy token to read and submit | host | yes | §9 |
| 10 | Watchdog | host | alerts | §10 |
| 11 | Remove the Codex agent config and 9Router credentials, with backups | host | host cleanup | §11 |

## 0. Merge, deploy procedure, deploy

On the laptop, in the merged checkout (`git -C <checkout> pull`):

```bash
MERGE_SHA=$(git rev-parse origin/main); echo "$MERGE_SHA"
grep -q '"llm": _mode("AIWIKI_LLM"' src/aiwiki/service/app.py && echo llm-switch
test -f skills/ai-wiki-auditor/SKILL.md && test -f docs/prompts/auditor-autopilot-prompt.md \
  && test -f docs/prompts/auditor-agent-instructions.md && echo auditor-package
AIWIKI_BUNDLES=$(mktemp -d) AIWIKI_TOKEN=probe AIWIKI_CURATE=off AIWIKI_INTAKE=inbox AIWIKI_AUDIT=external \
  AIWIKI_LLM=off AIWIKI_BACKLOG_EPOCH=2026-10-01T00:00:00Z uv run --extra service python -c \
  'from aiwiki.service import app; print(app.MODES["intake"], app.MODES["audit"], app.MODES["llm"])'
#   inbox external off: the build honours every final flag (it refuses to start on one it does not)
gh run list --branch main --limit 1 --json conclusion,headSha | jq -c '.[0]'   # success, headSha $MERGE_SHA
```

The checks print `llm-switch`, `auditor-package` and `inbox external off`; CI is green on
`$MERGE_SHA`. Then apply §10.2 (the deploy
procedure's lease guard and run windows) to `deploy_aliyun.sh`, and deploy:

```bash
cp deploy_aliyun.sh deploy_aliyun.sh.pre-final
deploy_aliyun.sh <checkout> "$MERGE_SHA"          # refuses while a writer job or lease is live
```

Verify (host): `cat /home/admin/app/.ai-wiki-deployed-revision` prints `$MERGE_SHA`, the
mirror image is `ai-wiki:…-<MERGE_SHA short>`, and §1 H3 shows today's modes plus `"llm":"codex"`.
Rollback: `deploy_aliyun.sh <checkout> 9d62536`. The new routes and flags are unused until
step 6, so the old build serves exactly as before.

## 1. Pre-flight

### Host (read-only, plus backups)

Define the helpers in every new root shell on aliyun-jp:

```bash
export PROD=/home/admin/solvely-wiki
export SHADOW=/var/lib/ai-wiki/bundles/solvely-wiki-shadow
export DROPIN=/etc/systemd/system/ai-wiki-worker.service.d
export FS=/root/ai-wiki-final
as_admin() { sudo -u admin "$@"; }
pp() { env PYTHONDONTWRITEBYTECODE=1 /home/admin/app/.venv/bin/python \
         /home/admin/app/scripts/provision_principals.py --file /etc/ai-wiki/principals.json "$@"; }
legacy_token() { systemctl show ai-wiki-worker -p Environment --value | tr ' ' '\n' | sed -n 's/^AIWIKI_TOKEN=//p'; }
hup() { kill -HUP "$(pgrep -P "$(systemctl show -p MainPID --value ai-wiki-worker)" -f aiwiki.service)"
        docker kill -s HUP ai-wiki >/dev/null; }
whoami_w() { curl -s -H @$FS/owner.h http://127.0.0.1:8788/whoami; }
status_w() { curl -s -H @$FS/owner.h "http://127.0.0.1:8788/maint/status?bundle=$1"; }
idle() {  # 0 when neither bundle has a queued or running job or a live lease
  for b in solvely-wiki solvely-wiki-shadow; do
    status_w $b | jq -e '(.jobs.queued + .jobs.running) == 0
                         and ([.leases[] | select(. != null and .active)] | length) == 0' >/dev/null || return 1
  done
}
codex_jobs() {  # queued or running legacy Codex jobs (ingest or audit, not changesets)
  as_admin /home/admin/app/.venv/bin/python - $PROD $SHADOW <<'PY'
import glob, json, sys
n = 0
for bundle in sys.argv[1:]:
    for path in glob.glob(f"{bundle}/.okf/jobs/*.json"):
        try:
            job = json.load(open(path))
        except Exception:
            continue
        n += job.get("status") in ("queued", "running") and job.get("mode") != "changeset" \
            and job.get("kind", "ingest") in ("ingest", "audit")
print(n)
PY
}
restart_idle() {
  if idle; then
    systemctl restart ai-wiki-worker
    for i in $(seq 60); do curl -fsS -o /dev/null -H @$FS/owner.h http://127.0.0.1:8788/whoami && break; sleep 2; done
    systemctl is-active ai-wiki-worker
  else
    echo 'STOP: a job or a lease is live; rerun when idle'
  fi
}
```

Once, the header files (the owner token comes from the password manager; a header file keeps
tokens out of `ps`):

```bash
install -d -m 0700 $FS
( umask 077; read -rs OWNER; printf 'Authorization: Bearer %s\n' "$OWNER" > $FS/owner.h
  printf 'Authorization: Bearer %s\n' "$(legacy_token)" > $FS/legacy.h )
```

Checks, each printing what its comment says:

```bash
# H1. The merged build on both services.
cat /home/admin/app/.ai-wiki-deployed-revision                              # $MERGE_SHA
docker inspect ai-wiki --format '{{.Config.Image}}'                         # ai-wiki:…-<MERGE_SHA short>
# H2. The writer's drop-ins are the Phase 2 set.
ls $DROPIN      # codex-runtime.conf okf-v02.conf phase1-principals.conf phase2-shadow.conf zz-agent-config.conf
# H3. The modes are today's.
whoami_w | jq -c '{writer, principal, modes}'
#   writer true, human:guobaoqi; intake curate, audit codex, changesets_commit ["solvely-wiki-shadow"],
#   codex_audit_manual ["solvely-wiki-shadow"], llm codex
# H4. The principals.
pp list | jq -r '.[] | "\(.id) \(.role) \(.bundles)"'
#   member:legacy-token admin ["solvely-wiki"]; human:guobaoqi admin null;
#   process:ai-wiki-maintainer-shadow curator ["solvely-wiki-shadow"];
#   process:ai-wiki-shadow-audit auditor ["solvely-wiki-shadow"];
#   process:ai-wiki-maintainer curator ["solvely-wiki","solvely-wiki-shadow"]
# H5. The tunnel routes the gate, the backlog and admin to the writer.
curl -s http://127.0.0.1:20242/config | jq -r '.config.ingress[0].path'
#   ^/(ingest|jobs|whoami|workspace|changesets|maint|audit/backlog|admin)
# H6. Production is clean, on main, and published.
as_admin git -C $PROD status --porcelain | wc -l                            # 0
as_admin git -C $PROD rev-parse HEAD origin/main                            # the same sha twice
# H7. The writer's agent configuration, as step 11 will find it (no secret is printed).
as_admin /home/admin/app/.venv/bin/python -c 'import json; print(json.load(open("/home/admin/.ai-wiki/config.json"))["agent"]["bin"])'
#   /home/admin/.local/bin/codex-9router
stat -c '%U:%G %a %n' /home/admin/.local/bin/codex-9router /home/admin/.config/secrets/codex-gateway.env
#   admin:admin 700 …codex-9router; admin:admin 600 …codex-gateway.env
# H8. Idle, and no Codex job waiting.
idle && echo idle; codex_jobs                                               # idle; 0
curl -s -H @$FS/legacy.h 'http://127.0.0.1:8788/jobs/pending-audit?bundle=solvely-wiki&older_than_hours=0' \
  | jq -c '{total, unscoped}'                                              # record it; step 6 hands these to the backlog
df -BG --output=avail /var/lib | tail -1                                    # >= 2G
```

If any check fails, stop. Then back up what later steps change:

```bash
cp -a $DROPIN $FS/worker.service.d.before
cp -a /etc/systemd/system/ai-wiki-watchdog.service /etc/systemd/system/ai-wiki-watchdog.service.d $FS/
cp -a /etc/ai-wiki/principals.json $FS/principals.json.before
cp -a /etc/systemd/system/ai-wiki-shadow-audit.service /etc/systemd/system/ai-wiki-shadow-audit.timer \
      /usr/local/sbin/ai-wiki-shadow-audit /etc/ai-wiki-shadow-audit.env /var/lib/ai-wiki/shadow-audit $FS/
whoami_w | jq -c .modes > $FS/modes.before.json
```

The Phase 2 run left the owner token on this host (`/root/ai-wiki-phase2/owner.token` and
`owner.h`); its §11.7 meant to remove them. Remove them now:
`shred -u /root/ai-wiki-phase2/owner.token /root/ai-wiki-phase2/owner.h`.

Rollback: none; it only reads and copies.

### Laptop (read-only, plus the archive)

```bash
export FL=~/ai-wiki-final; mkdir -p $FL && cd $FL
export PROD_AGENT=1dcccd34-e9e4-48c7-a0a3-32c061d4c284 PROD_AP=5c80732b-67a6-4e33-ba22-c620a94e27c1
export PROD_TRIGGER=d4fb8f06-6a19-461c-a31d-464c01212393
export SHADOW_AGENT=c10a1e06-8255-42ce-9b52-266aef42f2a6 SHADOW_AP=d455adf9-24d4-46c0-9da5-a7b8896faa4f
export CURATING_SKILL=9ee08f00-0300-4962-b6e0-7ab55a9d9066 OKF_SKILL=356d20e4-07cc-4f15-91b7-6d081dc43625
export AIWIKI_SKILL=e49bcda9-468c-4d89-a33c-4fcd356ac137 LEGACY_SKILL=bedcd57e-4d4b-48cc-be92-1be9da9facfa
export CHECKOUT=<the merged checkout>
multica agent get $PROD_AGENT --output json > prod-agent.before.json
multica autopilot get $PROD_AP --output json > prod-autopilot.before.json
multica agent get $SHADOW_AGENT --output json > shadow-agent.before.json
multica autopilot get $SHADOW_AP --output json > shadow-autopilot.before.json
jq -r .instructions prod-agent.before.json > prod-instructions.legacy.md
jq -r .autopilot.description prod-autopilot.before.json > autopilot-prompt.legacy.md
for id in $CURATING_SKILL $OKF_SKILL $AIWIKI_SKILL $LEGACY_SKILL; do
  multica skill get $id --with-content --output json > skill-$id.before.json
done
jq -c '{skills: [.skills[].name], max_concurrent_tasks, model, runtime_id}' prod-agent.before.json
#   ["ai-wiki","ai-wiki-maintainer"], 2, claude-opus-5-5-combos, df0fb673-…
jq -c '{status: .autopilot.status, cron: [.triggers[] | {id, cron_expression, timezone}]}' prod-autopilot.before.json
#   active, [{"id":"d4fb8f06-…","cron_expression":"0 4 * * *","timezone":"Asia/Shanghai"}]
multica runtime list --output json | jq -c '.[] | select(.id | startswith("df0fb673") or startswith("1ae1dfab"))
  | {id, name, status}'                                                      # both online
```

These files are the rollback of steps 3 and 5 (§13); keep them until step 11 is done.

### Runtime: issue R1, pin the CLI

Title `[OPS] AI Wiki final cut-over R1: pin the CLI`. Text (`$FL/issue-R1.md`, with
`<MERGE_SHA>` filled in):

```text
One-time operations task from the owner, not a maintenance run. In one bash shell on this host,
run exactly the commands below, in order, and nothing else: no maintain, ingest or audit, no
other installs, no edits to any file. Post one comment with every command and its complete
output verbatim, then set this issue to done with --no-start, or to blocked if a command failed.

find "$(uv tool dir)/ai-wiki" -path '*ai_wiki-*.dist-info/direct_url.json' -exec cat {} \;
uv tool install --force "git+https://github.com/Scorpion1221/ai-wiki@<MERGE_SHA>" && hash -r
ai-wiki -b solvely-wiki health --json
ai-wiki -b solvely-wiki doctor --role curator --json
ai-wiki maint end --help
```

Verify: the first line's `commit_id` is `PREV_CLI` (write it down); `health` shows
`compatible: true` and the merged `client_version`; `doctor` shows `"ok": true`; `maint end
--help` lists `--format`. Every agent on `df0fb673` shares this CLI; the legacy flow keeps
working with it (its `maintain`, `audit` and `jobs` verbs are unchanged), so the 04:00 run
before step 3 is the check. Rollback: the same issue with
`uv tool install --force "git+https://github.com/Scorpion1221/ai-wiki@<PREV_CLI>" && hash -r`.

### Members

Send the members the change for day 0: `ai-wiki ingest` works as before, but a submission is
curated by the maintainer's next run (04:00, 12:00 or 20:00 CST) instead of within minutes;
`ai-wiki jobs <id>` follows it. Nothing to reinstall.

## 2. The Auditor (day −1, no schedule)

**Principal** (host):

```bash
( umask 077; set -o noclobber; pp add auditor > $FS/auditor.token )   # process:ai-wiki-auditor, aiw_a_
pp check && hup
whoami_w | jq -c '.auth | {principals, reload_error}'                  # six ids, null
```

`pp check` (without `AIWIKI_TOKEN`) validates the file; also run `AIWIKI_TOKEN="$(legacy_token)"
pp check`: it must print `held by member:legacy-token`. The owner copies
`$FS/auditor.token` into the password manager; `shred -u $FS/auditor.token` once the agent
holds it (below).

**Skill** (laptop, from `$CHECKOUT`):

```bash
python3 scripts/sync_skills.py --apply ai-wiki-auditor && python3 scripts/sync_skills.py --check ai-wiki-auditor
multica skill create --name ai-wiki-auditor --description "<description from its SKILL.md frontmatter>" \
  --content-file ~/.agents/skills/ai-wiki-auditor/SKILL.md --output json | jq -r '.id // .skill.id'   # AUDITOR_SKILL
```

**Agent and autopilot** (laptop). Take the skills, model and settings the header table of
`docs/prompts/auditor-agent-instructions.md` names; the model must be of another family than
the maintainer's (`claude-opus-5-5-combos`). `AUDITOR_RUNTIME` is the full id of the chosen
runtime (`multica runtime list --output json | jq -r '.[] | select(.id | startswith("1ae1dfab")) | .id'`).

```bash
awk 'f;/^---$/{f=1}' docs/prompts/auditor-agent-instructions.md > $FL/auditor-instructions.md
read -rs AGENT_TOKEN                                                   # the aiw_a_ auditor token
printf '{"AIWIKI_TOKEN":"%s"}' "$AGENT_TOKEN" | multica agent create --name "AI Wiki Auditor" \
  --runtime-id "$AUDITOR_RUNTIME" --max-concurrent-tasks 1 --permission-mode private \
  --instructions "$(cat $FL/auditor-instructions.md)" --custom-env-stdin --output json | jq -r '.id // .agent.id'   # AUDITOR_AGENT
unset AGENT_TOKEN
multica agent update "$AUDITOR_AGENT" --model "<model from the header table>"
multica agent skills set "$AUDITOR_AGENT" --skill-ids "$AUDITOR_SKILL"   # plus any the header table lists
cp docs/prompts/auditor-autopilot-prompt.md $FL/auditor-prompt.md       # fill its placeholders per its header
multica autopilot create --title "AI Wiki audit" --agent "$AUDITOR_AGENT" --mode create_issue \
  --issue-title-template "[AUTO] AI Wiki audit {{date}}" \
  --description "$(cat $FL/auditor-prompt.md)" --output json | jq -r '.id // .autopilot.id'   # AUDITOR_AP, no trigger
```

**Auditor host.** The owner installs the CLI there once, as the runtime's OS user:
`uv tool install --force "git+https://github.com/Scorpion1221/ai-wiki@$MERGE_SHA" && hash -r`
and `ai-wiki config set --endpoint https://ai-wiki.yqbqnn.com/` (no token: the agent's env
brings it). Then issue A1, assigned to `$AUDITOR_AGENT`, title `[OPS] AI Wiki final cut-over
A1: auditor preflight`:

```text
One-time operations task from the owner, not an audit run. Run exactly these two commands and
nothing else; post one comment with both outputs verbatim; set this issue to done with
--no-start, or to blocked if a command failed.

ai-wiki -b solvely-wiki doctor --role auditor --json
ai-wiki -b solvely-wiki health --json
```

Verify: `doctor` `"ok": true` (scopes exactly read and audit; `git`, `uv` present), `health`
`compatible: true`. Until step 6 audit changesets are refused (`AIWIKI_AUDIT=codex`), so the
agent can do nothing else yet. Rollback: `multica autopilot delete $AUDITOR_AP`,
`multica agent archive $AUDITOR_AGENT`, `multica skill delete $AUDITOR_SKILL`, and on the host
`pp remove process:ai-wiki-auditor; pp check; hup`.

## 3. Retire the shadow agent, then stop the legacy run

### 3a. The shadow agent (day −1, after the shadow's 13:30 run)

Laptop, once the shadow's 13:30 CST issue is done:

```bash
multica autopilot update $SHADOW_AP --status paused --output json | jq -r '.status // .autopilot.status'   # paused
multica agent archive $SHADOW_AGENT --output json | jq -r '.archived_at // .agent.archived_at'              # a timestamp
```

Host: the shadow's Codex audit timer, its principal, and the watchdog's shadow bundle go
(the shadow's cursors and commits stop moving, so its checks would page):

```bash
systemctl disable --now ai-wiki-shadow-audit.timer
rm /etc/systemd/system/ai-wiki-shadow-audit.{service,timer} /usr/local/sbin/ai-wiki-shadow-audit /etc/ai-wiki-shadow-audit.env
rm -rf /var/lib/ai-wiki/shadow-audit
mv /etc/systemd/system/ai-wiki-watchdog.service.d/phase2-shadow.conf $FS/watchdog-phase2-shadow.conf
systemctl daemon-reload
pp remove process:ai-wiki-shadow-audit && pp check && hup
systemctl show ai-wiki-watchdog -p ExecStart --value | grep -c solvely-wiki-shadow        # 0
systemctl list-timers --all --no-pager 'ai-wiki*' | grep -c shadow-audit                  # 0
```

**The shadow bundle stays** (recommendation). Keep `solvely-wiki-shadow`, its read clone and
pull timer, and `process:ai-wiki-maintainer-shadow` (its token stays in the archived agent's
env). It is the canary bundle: before a model, runtime or effort change the maintainer runs
there first (docs/external-agents.md §6), and `admin compare` keeps its history. It costs a
5-minute pull and about 10 MB, and nothing alerts on it once the watchdog stops watching it.
To remove it instead, run the Phase 2 runbook's §12 R2 and R4 blocks after step 6, with
`solvely-wiki` alone in `AIWIKI_CHANGESETS_COMMIT`.

Rollback: `multica agent restore $SHADOW_AGENT`, `multica autopilot update $SHADOW_AP --status
active`, `mv $FS/watchdog-phase2-shadow.conf /etc/systemd/system/ai-wiki-watchdog.service.d/phase2-shadow.conf`
and `systemctl daemon-reload`, and the audit timer as the Phase 2 runbook §9 installs it, from
the files in `$FS` with a new token (`pp add auditor --id process:ai-wiki-shadow-audit --bundle
solvely-wiki-shadow`); until step 6 only (an external audit mode refuses its requests).

### 3b. The legacy run (day 0, after the 04:00 run)

Laptop, once the 04:00 CST run's issue is done:

```bash
multica autopilot runs $PROD_AP --limit 1 --output json | jq -c '.runs[0] | {status, issue_id, created_at}'   # completed
multica autopilot update $PROD_AP --status paused --output json | jq -r '.status // .autopilot.status'     # paused
```

The legacy run may leave a background `ai-wiki maintain` and queued Codex jobs behind: on the
host, wait until `codex_jobs` prints 0 and `idle && echo idle` prints `idle` (issue R2 also
refuses while a `maintain` still runs). Rollback: `multica autopilot update $PROD_AP --status active`.

## 4. Import the cursors and the ledger (issue R2)

Title `[OPS] AI Wiki final cut-over R2: import cursors and ledger`, assigned to
`$PROD_AGENT` (still on its legacy instructions, whose skill directory holds
`checkpoint.py`). Text:

```text
One-time operations task from the owner, not a maintenance run: seed the writer's cursors from
production's latest v4 checkpoint and its unfinished ledger sources. In one bash shell, run
exactly the commands below, in order, and nothing else: no maintain, ingest or audit, and no
edit to state.json, a checkpoint or any other file. Stop at the first failure. Post one comment
with every command and its complete output verbatim, then set this issue to done with
--no-start, or to blocked if you stopped.

flock -n "${XDG_STATE_HOME:-$HOME/.local/state}/ai-wiki-maintainer/solvely-wiki/runner.lock" true || echo 'STOP: a legacy maintain is still running'
SKILL_DIR="${AI_WIKI_MAINTAINER_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/ai-wiki-maintainer}"
[ -f "$SKILL_DIR/scripts/checkpoint.py" ] || SKILL_DIR="$HOME/.agents/skills/ai-wiki-maintainer"
mkdir -p /tmp/ai-wiki-final
python3 "$SKILL_DIR/scripts/checkpoint.py" find --autopilot 5c80732b-67a6-4e33-ba22-c620a94e27c1 --seed-issue 01a063b5-62c8-7f34-8901-580e722d9532 --cache-dir /tmp/ai-wiki-final/find-cache --output /tmp/ai-wiki-final/find.json; echo "find exit $?"
ai-wiki -b solvely-wiki maint import-v4 /tmp/ai-wiki-final/find.json --run final-cutover-R2 --json
ai-wiki -b solvely-wiki maint import-ledger --ledger "${XDG_STATE_HOME:-$HOME/.local/state}/ai-wiki-maintainer/solvely-wiki" --json
ai-wiki -b solvely-wiki maint status --json

Stop without running the rest if the first line prints STOP, or if find exits with anything but 0.
```

Verify from the comment: `find exit 0` and its `completed_at` is the 04:00 run's; import-v4
rows `repos` and `issues` `created`; import-ledger `imported` (items `created`), with
`in_flight` empty (else a legacy ingest was still landing: roll back and rerun later) and
`audit_pending` listed (their concepts reach the Auditor's backlog in step 6); `maint status`
shows both cursors with `run` `final-cutover-R2` and the ready items. On the host,
`status_w solvely-wiki | jq -c '{cursors, items}'` shows the same.

Rollback (host, when `status_w solvely-wiki | jq '.leases'` shows no live lease):
`as_admin mv $PROD/.okf/maint $PROD/.okf/maint.unimported-$(date +%F)`. The ledger was only
read; the legacy flow resumes from it unchanged.

## 5. Switch the production agent (laptop)

Skills first. The shadow already runs the curating skill; publish the merged versions (the
`ai-wiki` skill is shared with other agents, and its new text describes the final state):

```bash
cd $CHECKOUT
python3 scripts/sync_skills.py --apply ai-wiki ai-wiki-curating-maintainer okf-knowledge-curator
python3 scripts/sync_skills.py --check ai-wiki ai-wiki-curating-maintainer okf-knowledge-curator   # all OK
multica skill update $CURATING_SKILL --content-file ~/.agents/skills/ai-wiki-curating-maintainer/SKILL.md
multica skill update $OKF_SKILL --content-file ~/.agents/skills/okf-knowledge-curator/SKILL.md
multica skill update $AIWIKI_SKILL --content-file ~/.agents/skills/ai-wiki/SKILL.md
```

Then the agent and its autopilot. `AUDITOR_AGENT` comes from step 2:

```bash
awk 'f;/^---$/{f=1}' docs/prompts/production-agent-instructions.md > $FL/prod-instructions.md
sed "s/<Auditor agent id>/$AUDITOR_AGENT/" docs/prompts/production-autopilot-prompt.md > $FL/prod-prompt.md
grep -c '<Auditor agent id>' $FL/prod-prompt.md                                   # 0
multica agent skills set $PROD_AGENT --skill-ids $CURATING_SKILL,$OKF_SKILL
multica agent update $PROD_AGENT --instructions "$(cat $FL/prod-instructions.md)" --max-concurrent-tasks 1
multica autopilot update $PROD_AP --title "AI Wiki maintainer" \
  --issue-title-template "[AUTO] AI Wiki sync {{date}}" --description "$(cat $FL/prod-prompt.md)"
multica autopilot trigger-update $PROD_AP $PROD_TRIGGER --cron "0 4,12,20 * * *" \
  --timezone Asia/Shanghai --label "04:00/12:00/20:00 Asia/Shanghai"
```

Set `max_attempts=2` and a 3 h task timeout in the UI or runtime config, if exposed. The
agent keeps its model, runtime and `AIWIKI_TOKEN` (`process:ai-wiki-maintainer`, the curator
role), and the autopilot stays paused.

Verify:

```bash
multica agent get $PROD_AGENT --output json | jq -c '{skills: [.skills[].name], max_concurrent_tasks,
  instructions: (.instructions | contains("You are the AI Wiki Maintainer. You curate"))}'
#   ["ai-wiki-curating-maintainer","okf-knowledge-curator"], 1, true
multica autopilot get $PROD_AP --output json | jq -c '{status: .autopilot.status,
  prompt: (.autopilot.description | contains("ai-wiki-curating-maintainer")), cron: [.triggers[].cron_expression]}'
#   paused, true, ["0 4,12,20 * * *"]
```

Rollback (the archive of step 1):

```bash
cd $FL
multica agent skills set $PROD_AGENT --skill-ids $AIWIKI_SKILL,$LEGACY_SKILL
multica agent update $PROD_AGENT --instructions "$(cat prod-instructions.legacy.md)" --max-concurrent-tasks 2
multica autopilot update $PROD_AP --title "AI Wiki daily incremental sync" \
  --issue-title-template "[AUTO] AI Wiki daily sync {{date}}" --description "$(cat autopilot-prompt.legacy.md)"
multica autopilot trigger-update $PROD_AP $PROD_TRIGGER --cron "0 4 * * *" --timezone Asia/Shanghai \
  --label "Daily 04:00 Asia/Shanghai"
```

The skill contents roll back from `skill-<id>.before.json` as in the Phase 2 runbook §11.3.

## 6. Writer drop-in: the final flags (host)

`AIWIKI_LLM` stays `codex` here: step 8 flips it once the canary has passed, so until then a
rollback can still hand member items back to Codex. The drop-in sorts after
`phase2-shadow.conf`, which it overrides:

```bash
EPOCH=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "$EPOCH" > $FS/backlog-epoch
cat > $DROPIN/phase3-final.conf <<EOF
# Final state (docs/final-cutover-runbook.md step 6). Sorts after phase2-shadow.conf.
[Service]
Environment=AIWIKI_CHANGESETS_COMMIT=solvely-wiki,solvely-wiki-shadow
Environment=AIWIKI_INTAKE=inbox
Environment=AIWIKI_AUDIT=external
Environment=AIWIKI_BACKLOG_EPOCH=$EPOCH
Environment=AIWIKI_CODEX_AUDIT_MANUAL=
EOF
systemctl daemon-reload
[ "$(codex_jobs)" = 0 ] && restart_idle || echo 'STOP: a Codex job is queued or running'
```

On `STOP`, rerun the last line once idle: the drop-in takes effect only at that restart.

Verify:

```bash
systemctl is-active ai-wiki-worker                                                   # active
whoami_w | jq -c '.modes | {intake, audit, changesets_commit, codex_audit_manual, llm}'
#   {"intake":"inbox","audit":"external","changesets_commit":["solvely-wiki","solvely-wiki-shadow"],
#    "codex_audit_manual":[],"llm":"codex"}
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H @$FS/owner.h \
  'http://127.0.0.1:8788/jobs/no-such-job/audit?bundle=solvely-wiki'                # 409: audit is external
curl -s -H @$FS/owner.h 'http://127.0.0.1:8788/audit/backlog?bundle=solvely-wiki&limit=5' | jq -c 'keys'   # the backlog answers
curl -s -H @$FS/owner.h 'http://127.0.0.1:8788/health?bundle=solvely-wiki' | jq -c '{bundle, concepts, build}'
journalctl -u ai-wiki-worker --since -10min --no-pager | grep -iE 'traceback|error' || echo clean
```

Rollback, when idle: `rm $DROPIN/phase3-final.conf && systemctl daemon-reload && restart_idle`;
`/whoami` shows today's modes again (compare with `$FS/modes.before.json`). Member items that
arrived meanwhile go back to Codex with W14's `POST /admin/inbox/requeue` (owner token, see
its route docstring for the body); changesets already committed are valid OKF content and stay.

## 7. Canary: one item, one auditor run, then full

**7a. The maintainer, one item** (laptop). The production prompt with `max_items=1`, as a
one-time issue for the production agent:

```bash
cd $FL && sed 's/^max_items=6 /max_items=1 /' prod-prompt.md > canary-prompt.md
grep -c '^max_items=1 ' canary-prompt.md                                                # 1
multica issue create --title "[CANARY] AI Wiki sync, 1 item" --assignee-id $PROD_AGENT \
  --description-file canary-prompt.md --output json | jq -r '.id // .issue.id'          # CANARY_ISSUE
```

When it is done, on the laptop with the owner token and a throwaway CLI config:

```bash
export AIWIKI_CONFIG=$(mktemp -d)/config.json
echo '{"endpoint": "https://ai-wiki.yqbqnn.com/"}' > $AIWIKI_CONFIG
read -rs AIWIKI_TOKEN && export AIWIKI_TOKEN                                            # owner, aiw_h_
multica issue comment list $CANARY_ISSUE --output json | jq -r '(.comments // .)[-1].content' | head -1
#   AI Wiki maintenance <run> (…) status=done: the report opens the comment
cd $CHECKOUT
uv run ai-wiki -b solvely-wiki admin changesets --limit 5 --json \
  | jq -c '.changesets[] | {id, status, commit, concept_files}'                        # the canary's, done; none if it skipped
uv run ai-wiki -b solvely-wiki jobs <changeset id> --json | jq -c '{status, commit, validation, audit, git}'
#   done, the commit, validation passed, audit {"mode":"external"}, pushed true
ws=$(mktemp -d); git clone -q --depth 1 git@code.ddit.ai:solvely-web/solvely-web-ai-wiki.git $ws/clone
git -C $ws/clone log -1 --format=%H                                                     # the changeset's commit (or later)
uv run okf-validate $ws/clone && echo clone-valid
uv run ai-wiki -b solvely-wiki cat <a concept_file> --json | jq -c '.metadata | {status, trust, verification_current}'
#   within 10 minutes (the mirror pulls every 5): draft or verification_current false
uv run ai-wiki -b solvely-wiki log --tail 5                                              # the changeset's log line
```

If the item was skipped (no durable knowledge), the report says so and there is no
changeset: run 7a once more. On the host, a replay of the watchdog shows no alert:
`as_admin /home/admin/app/.venv/bin/python /usr/local/bin/ai-wiki-watchdog --bundle $PROD --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | jq -c '{status, alerts: [.alerts[].key]}'`.

**7b. One auditor run** (laptop): `multica autopilot trigger $AUDITOR_AP`. When its issue is
done, the canary's concept was reviewed:

```bash
uv run ai-wiki -b solvely-wiki cat <the concept_file> --json | jq -c '.metadata | {status, trust, verification_current}'
#   stable; machine-confirmed and true (verified or a corrected narrowing), or unverified with the reviewer's note
git -C $ws/clone pull -q && git -C $ws/clone log -3 --format='%h %s'                   # the audit changeset's commit
grep -A3 '^verified:' $ws/clone/<the concept_file>                                      # by: process:ai-wiki-auditor, never the maintainer
```

**7c. Full** (laptop):

```bash
multica autopilot update $PROD_AP --status active --output json | jq -r '.status // .autopilot.status'   # active
multica autopilot trigger-add $AUDITOR_AP --kind schedule --cron "0 7,15 * * *" \
  --timezone Asia/Shanghai --label "07:00/15:00 Asia/Shanghai"
multica autopilot get $PROD_AP --output json | jq -c '[.triggers[] | {cron_expression, next_run_at}]'
```

Stop and roll back (§13) at once if the gate is bypassed (a curator's changeset carries a
`verified` event), a receipt disagrees with origin, the fresh clone fails `okf-validate`, two
scheduled runs in a row leave the cursors where they were, or the owner finds two serious
errors in one run. After each scheduled run for the first days: the issue's first comment
line, `ai-wiki -b solvely-wiki maint status --json`, and the watchdog's hourly result.

## 8. The writer's Codex off (host)

After step 7c and two clean scheduled maintainer runs. Nothing in the final flags starts
Codex any more; this makes it impossible:

```bash
codex_jobs                                                                    # 0, else wait or clear them first
cat > $DROPIN/phase3-llm-off.conf <<'EOF'
# The writer never starts an agent process (docs/final-cutover-runbook.md step 8).
[Service]
Environment=AIWIKI_LLM=off
EOF
systemctl daemon-reload && restart_idle
```

Verify:

```bash
whoami_w | jq -r .modes.llm                                                              # off
curl -s -H @$FS/owner.h 'http://127.0.0.1:8788/health?bundle=solvely-wiki' | jq -c .writer_agent   # {"runtime":"off"}
systemd-cgls --no-pager -u ai-wiki-worker.service | grep -c codex                        # 0: nothing but uv and python
```

Rollback: `rm $DROPIN/phase3-llm-off.conf && systemctl daemon-reload && restart_idle`, and
`.modes.llm` reads `codex` again. The Codex configuration is still on the host until step 11.

## 9. Members: the legacy token to read and submit (host)

`member:legacy-token` (the shared `eb17…`) still holds every scope on `solvely-wiki`. In the
final state members only read and submit:

```bash
jq '(.principals[] | select(.id == "member:legacy-token")) .scopes = ["read", "submit"]' \
   /etc/ai-wiki/principals.json > /etc/ai-wiki/.principals.json.new
chown root:admin /etc/ai-wiki/.principals.json.new && chmod 0640 /etc/ai-wiki/.principals.json.new
mv /etc/ai-wiki/.principals.json.new /etc/ai-wiki/principals.json
AIWIKI_TOKEN="$(legacy_token)" pp check && hup
curl -s -H @$FS/legacy.h http://127.0.0.1:8788/whoami | jq -c '{principal, scopes, role}'
#   member:legacy-token, ["read","submit"], member
```

Rollback: the same `jq` with `.scopes = ["admin","audit","curate","human_verify","read","submit"]`,
then `pp check` and `hup`. Per-member `aiw_m_` tokens (`pp add member --id member:<name>`)
replace the shared one later; that rotation is not part of this cut-over.

## 10. Watchdog and deploy procedure

### 10.1 Watchdog (host)

The writer-host unit is the whole final-state watchdog: `--bundle $PROD` now reads the
production cursors and queue. Install the merged script if it changed, and replay:

```bash
cmp -s /home/admin/app/scripts/maintenance_watchdog.py /usr/local/bin/ai-wiki-watchdog || echo changed
```

The deploy puts the merged tree in `/home/admin/app`. If it changed, as in the Phase 2
runbook plan step 4: back up `/usr/local/bin/ai-wiki-watchdog`,
`install -m 0755 -o root -g root /home/admin/app/scripts/maintenance_watchdog.py /usr/local/bin/ai-wiki-watchdog`,
then:

```bash
systemctl show ai-wiki-watchdog -p ExecStart --value | grep -o -- '--bundle [^ ]*'       # --bundle /home/admin/solvely-wiki only
as_admin /home/admin/app/.venv/bin/python /usr/local/bin/ai-wiki-watchdog --bundle $PROD \
  --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | jq -c '{status, cursors: .checks["maint:solvely-wiki"].cursors, alerts: [.alerts[].key]}'
#   ok; repos and issues with their ages; no alert
```

What pages from now on: a cursor that has not advanced for 30 h (the maintainer did not run
three times), an item waiting over 72 h or needing a human, a run lease stuck over 3 h, no
commit for 48 h, and writer job failures. `--multica` stays off: its checks read the legacy
v4 checkpoint, which the curating maintainer does not write. The Auditor's backlog age has no
watchdog check yet; read `/audit/backlog` in the daily look of step 7 until one exists.
Rollback: install the backed-up script; the shadow's `--bundle` comes back with §3's rollback.

### 10.2 Deploy procedure (laptop, `deploy_aliyun.sh`)

The deploy script lives outside this repository. Apply before step 0's deploy:

1. **Idle guard**: besides the queued or running jobs of both bundles, refuse while a
   maintainer or auditor lease is live. Leases are files the writer removes on release:

   ```bash
   ssh aliyun-jp sudo -u admin /home/admin/app/.venv/bin/python - <<'PY' || { echo 'writer busy'; exit 1; }
   import glob, json, sys
   from datetime import datetime, timezone
   now, busy = datetime.now(timezone.utc), []
   for bundle in ("/home/admin/solvely-wiki", "/var/lib/ai-wiki/bundles/solvely-wiki-shadow"):
       for path in glob.glob(f"{bundle}/.okf/jobs/*.json"):
           if json.load(open(path)).get("status") in ("queued", "running"):
               busy.append(path)
       for path in glob.glob(f"{bundle}/.okf/maint/lease-*.json"):
           expires = json.load(open(path)).get("expires_at") or ""
           if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) > now:
               busy.append(path)
   print("\n".join(busy))
   sys.exit(1 if busy else 0)
   PY
   ```

2. **Run windows**: refuse from 30 minutes before to 2 hours after 04:00, 12:00 and 20:00
   CST (maintainer) and 30 minutes around 07:00 and 15:00 CST (auditor starts), besides the
   existing 03:30–06:30 rule.
3. **After the deploy**, compare the writer's `/whoami` `.modes` with the ones before it,
   read with the token the script already uses for its health checks: the flags live in
   drop-ins, not the app directory, so a deploy must not change them.
4. Mirror: unchanged (it keeps the live container's mounts, Phase 2 runbook §6).

Rollback: `deploy_aliyun.sh.pre-final`.

## 11. Remove the Codex agent config and 9Router credentials (host, after 14 clean days)

After two weeks of the final state without a rollback (design §9 phase 5), take the Codex
configuration off the writer, with a backup. `/home/admin/.codex` stays: it is admin's own
interactive Codex login (a `codex` session and the `codex-auth` daemon use it), not the
writer's.

```bash
whoami_w | jq -r .modes.llm                                                   # off, else stop: step 8 first
grep -rlsE 'codex-gateway|codex-9router' /home/admin/.bashrc /home/admin/.profile /home/admin/.codex/config.toml \
  && echo 'STOP: something else uses the 9Router wrapper or its secret; keep them' || echo unused-elsewhere
```

Only after `off` and `unused-elsewhere`:

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ); BK=$FS/codex-host-$TS; install -d -m 0700 $BK
cp -a $DROPIN $BK/worker.service.d
tar -C /home/admin -czpf $BK/admin-codex.tgz .ai-wiki/config.json .local/bin/codex-9router .config/secrets/codex-gateway.env
chmod 600 $BK/admin-codex.tgz && tar -tzf $BK/admin-codex.tgz          # the three files
mv $DROPIN/codex-runtime.conf $DROPIN/zz-agent-config.conf $BK/
systemctl daemon-reload
as_admin shred -u /home/admin/.config/secrets/codex-gateway.env
as_admin rm /home/admin/.local/bin/codex-9router /home/admin/.ai-wiki/config.json
restart_idle
```

Verify:

```bash
ls $DROPIN                                    # okf-v02.conf phase1-principals.conf phase2-shadow.conf phase3-final.conf phase3-llm-off.conf
systemctl show ai-wiki-worker -p Environment --value | tr ' ' '\n' | grep -cE '^AIWIKI_(AGENT_|CONFIG=)'   # 0
whoami_w | jq -r .modes.llm                   # off
curl -s -H @$FS/owner.h 'http://127.0.0.1:8788/health?bundle=solvely-wiki' | jq -c .writer_agent   # {"runtime":"off"}
test ! -e /home/admin/.config/secrets/codex-gateway.env && test ! -e /home/admin/.local/bin/codex-9router && echo removed
```

The backup holds a live 9Router key: keep `$BK` root-only for 30 days, then `shred -u
$BK/admin-codex.tgz`. The owner may revoke that key at 9Router; a rollback past step 8 then
needs a new one. Rollback:

```bash
tar -C /home/admin -xzpf $BK/admin-codex.tgz
mv $BK/codex-runtime.conf $BK/zz-agent-config.conf $DROPIN/ && systemctl daemon-reload
```

and then step 8's rollback (a writer with `AIWIKI_LLM=off` reads none of it).

Finally, `shred -u $FS/owner.h $FS/legacy.h` on the host, and `unset AIWIKI_TOKEN` on the laptop.

## 12. What stays, what goes

- Stays: the gate (`runtime/changeset.py`, `policy.py`, the `_transaction` pipeline),
  recovery, receipts, the collectors, the shadow bundle as the canary target.
- Rollback-only until the Codex code is deleted (design W19): the legacy Codex runner, `POST
  /jobs/{id}/audit`, `/jobs/pending-audit`, `ai-wiki maintain` and `audit`, the
  `ai-wiki-maintainer` skill, the archive in `$FL`, the legacy ledger on the runtime host
  (`~/.local/state/ai-wiki-maintainer/solvely-wiki`, read-only from step 4 on).
- Docs: `docs/prompts/shadow-*.md` describe the retired shadow agent and serve its canaries.

## 13. Rollback to the legacy path

Each step's own rollback is listed with it. To go back to the legacy flow from any point
after step 5, in this order (skip what never ran):

1. **Stop the new runs** (laptop): `multica autopilot update $PROD_AP --status paused` and
   `multica autopilot update $AUDITOR_AP --status paused`. Wait until the host's `idle` passes.
2. **The writer back to Codex** (host): step 11's rollback if it ran, then
   `rm -f $DROPIN/phase3-llm-off.conf $DROPIN/phase3-final.conf && systemctl daemon-reload && restart_idle`.
   `whoami_w | jq -c .modes` equals `$FS/modes.before.json`. Hand member items back to Codex
   with `POST /admin/inbox/requeue` (step 6's rollback).
3. **The agent back to the legacy flow** (laptop): step 5's rollback block.
4. **The cursors and unfinished items back to the ledger** (issue RB1 below): the writer's
   cursors as a v4 checkpoint on the newest run issue, so the legacy `find` picks them up, and
   the unfinished work items as a `maintain` manifest.
5. **Resume the legacy schedule** (laptop): `multica autopilot update $PROD_AP --status active`;
   its next run starts from RB1's checkpoint.
6. **Archive the queue** (host, once `status_w solvely-wiki | jq .leases` shows no live
   lease), so the watchdog stops paging on cursors nothing advances:
   `as_admin mv $PROD/.okf/maint $PROD/.okf/maint.rolled-back-$(date +%F)`.
7. The Auditor stays paused (`multica agent archive $AUDITOR_AGENT` to retire it); committed
   changesets and audit changesets are valid OKF content and stay.

Issue RB1, title `[OPS] AI Wiki rollback RB1: export cursors to the legacy ledger`, assigned
to `$PROD_AGENT` after step 3 of this list restored its legacy skills; `<LAST_RUN_ISSUE>` is
`multica autopilot runs $PROD_AP --limit 1 --output json | jq -r '.runs[0].issue_id'`:

```text
One-time operations task from the owner: hand the curating maintainer's progress back to the
legacy ledger. In one bash shell run exactly the commands below, in order, and nothing else;
stop at the first failure. Post one comment with every command and its complete output
verbatim, then set this issue to done with --no-start, or to blocked if you stopped.

SKILL_DIR="${AI_WIKI_MAINTAINER_SKILL_DIR:-${CODEX_HOME:-$HOME/.codex}/skills/ai-wiki-maintainer}"
[ -f "$SKILL_DIR/scripts/checkpoint.py" ] || SKILL_DIR="$HOME/.agents/skills/ai-wiki-maintainer"
mkdir -p /tmp/ai-wiki-rollback
ai-wiki -b solvely-wiki maint status --json
ai-wiki -b solvely-wiki maint export-v4 --config "$HOME/.ai-wiki/maint-solvely-wiki.json" --output /tmp/ai-wiki-rollback/v4.json --pending-manifest /tmp/ai-wiki-rollback/sources.json --json
python3 "$SKILL_DIR/scripts/checkpoint.py" write --issue <LAST_RUN_ISSUE> --file /tmp/ai-wiki-rollback/v4.json
ai-wiki -b solvely-wiki maintain --manifest /tmp/ai-wiki-rollback/sources.json --state-dir "${XDG_STATE_HOME:-$HOME/.local/state}/ai-wiki-maintainer/solvely-wiki" --import-only --json

Stop before the export if maint status shows a maintainer lease with "active": true.
```

Verify: `write` reads the checkpoint back identical; `maintain --import-only` lists every
exported item as a frozen source. The next legacy run's `find` reports that checkpoint's
`completed_at`.

## Open risks

- The runbook names W14's and W16/W17's routes, flags, verbs and files (`/admin/inbox/requeue`,
  `/audit/backlog`, `AIWIKI_BACKLOG_EPOCH`, `ai-wiki-auditor`, `docs/prompts/auditor-*.md`) as
  the design specifies them; check them against the merged build before day −1.
- The first days of `AIWIKI_AUDIT=external` release the old unverified concepts into the
  backlog a few a day (the seed), so the Auditor's backlog stays long for a while; the
  maintainer is not affected.
- `{{date}}` is the UTC date: two of the three daily runs share an issue title. A run still
  open when the next starts would meet an active issue of the same title.
- The writer still runs as `admin`, the user of an interactive Codex login on the same host.
  `AIWIKI_LLM=off` guarantees the service never starts an agent; moving the writer to its own
  user (design §8.4) is the host hardening that remains.
