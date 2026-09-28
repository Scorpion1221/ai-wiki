# Final cut-over runbook: the server runs no LLM

This runbook takes production from today's legacy path (ingest, Codex curation on the
writer, Codex audit on the writer, a ledger checkpoint) straight to the final state: every
model-driven step runs in an external agent that holds a credential, and the writer is only
the deterministic gate. It needs no Codex, no model gateway and no Feishu login on the
server, and any agent with a principal of the right role can maintain, audit or submit
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

Step 6 sets them all in one drop-in. Step 11 then takes the Codex configuration, the model
gateway credential and admin's lark-cli login off the writer host, with backups.

Every step below has its verification and its rollback; §13 rolls back to the legacy path
from any point. Until step 11, rolling back is removing the step 6 drop-in and restoring
Multica settings from the archive step 1 makes; after it, restore step 11's backup first.

Requires the merged build of the three final-state units: W14 (inbox intake,
`/admin/inbox/requeue`), W16/W17 (audit changesets, `/audit/backlog`,
`AIWIKI_BACKLOG_EPOCH`, the `review` verbs, `skills/ai-wiki-auditor`,
`docs/prompts/auditor-*.md`) and this package (`AIWIKI_LLM`, the production prompts,
the watchdog's `--no-checkpoint` and member check, this runbook). Step 0's checks refuse a
build without them.

## Where and when

- **laptop**: the owner's machine, with a checkout of the merged build, `multica` logged in,
  `jq`, and the owner token in the password manager. Laptop files go to `$FL`.
- **host**: aliyun-jp as root in bash (`ssh aliyun-jp`), with the §1 helpers defined.
  Backups and header files go to `$FS` (root, 0700).
- **runtime**: ip-10-2-192-225, runtime `df0fb673`, reachable only through one-time Multica
  issues. Each such issue below is assigned to the production agent `1dcccd34` (its custom
  env holds the `process:ai-wiki-maintainer` token those steps need) and its exact text is
  given. Create it from `$FL` on the laptop, and mark it at once:

  ```bash
  cd $FL && multica issue create --title "<title>" --assignee-id $PROD_AGENT --description-file <file> \
    --output json | jq -r '.id // .issue.id'                                   # the issue id
  multica issue metadata set <issue-id> --key ai_wiki_ops --value true
  ```

  Every one-time issue of this runbook, the canary included, carries `ai_wiki_ops`: the
  legacy issue delta and the new issues collector both skip issues with `ai_wiki_*`
  metadata, so cut-over commands and their output never become wiki sources. Read the
  agent's comment with `multica issue comment list <issue-id> --output json`.
- **auditor host**: the host of the Auditor agent's runtime, `AUDITOR_RUNTIME`. The owner's
  default is Codex (GPT) on the Codex Gateway runtime; it qualifies only if step 2's
  isolation gate passes, which a runtime under the maintainer's OS user on the maintainer's
  host never does (docs/external-agents.md §1). Then take `Codex (macminim4.local)`
  (`1ae1dfab`, owner-managed; confirm it is always on) or run the gateway's daemon under
  another OS user.

Day −1 (owner online): steps 0 to 2, none of which changes production's behaviour. Day 0:
step 3 right after the 04:00 CST legacy run finishes, then 4 to 11 in one sitting (about four
hours, step 6 not before 06:30 CST). There is no waiting period: the canary (step 8) is the
gate, and step 11's backups stay 30 days for a rollback. Never restart the writer between
03:30 and 06:30 CST, nor while a job or lease is live (`restart_idle` refuses those).

## Plan

| # | Step | Where | Changes production | Rollback |
|---|---|---|---|---|
| 0 | Merge, deploy procedure, deploy the build | laptop, host | no: every new flag defaults to today | redeploy `9d62536` |
| 1 | Pre-flight, archive, member notice | host, laptop | no | none |
| 2 | Auditor (principal, skill, agent, autopilot without schedule); runtime CLI and isolation gate (issues R1, A1) | host, laptop, both runtimes | no | §2 |
| 3 | Pause the legacy and shadow runs, stop the shadow's Codex audit timer | laptop, host | schedules paused | §3 |
| 4 | Import the cursors and the ledger (issue R2) | runtime | writer queue seeded | §4 |
| 5 | Switch the production agent: skills, instructions, prompt (still paused) | laptop | agent config | §5 |
| 6 | Writer drop-in: every final flag, `AIWIKI_LLM=off` included | host | yes | §6 |
| 7 | Members: the legacy token to read and submit | host | yes | §7 |
| 8 | Canary: 1 item, one auditor run, then the schedules | laptop, host | yes | §13 |
| 9 | Retire the shadow agent | laptop, host | no | §9 |
| 10 | Watchdog once a day at 07:00: writer side, Multica side (issue R3); deploy procedure | host, runtime, laptop | alerts | §10 |
| 11 | Remove the Codex config, the gateway credential and the lark-cli login, with backups | host | host cleanup | §11 |

## 0. Merge, deploy procedure, deploy

On the laptop, in the merged checkout (`git -C <checkout> pull`):

```bash
MERGE_SHA=$(git rev-parse origin/main); echo "$MERGE_SHA"
grep -q '"llm": _mode("AIWIKI_LLM"' src/aiwiki/service/app.py && echo llm-switch
grep -q -- '--no-checkpoint' scripts/maintenance_watchdog.py && echo watchdog-final
test -f skills/ai-wiki-auditor/SKILL.md && test -f docs/prompts/auditor-autopilot-prompt.md \
  && test -f docs/prompts/auditor-agent-instructions.md && echo auditor-package
AIWIKI_BUNDLES=$(mktemp -d) AIWIKI_TOKEN=probe AIWIKI_CURATE=off AIWIKI_INTAKE=inbox AIWIKI_AUDIT=external \
  AIWIKI_LLM=off AIWIKI_BACKLOG_EPOCH=2026-10-01T00:00:00Z uv run --extra service python -c \
  'from aiwiki.service import app; print(app.MODES["intake"], app.MODES["audit"], app.MODES["llm"])'
#   inbox external off: the build honours every final flag (it refuses to start on one it does not)
gh run list --branch main --limit 1 --json conclusion,headSha | jq -c '.[0]'   # success, headSha $MERGE_SHA
```

The checks print `llm-switch`, `watchdog-final`, `auditor-package` and `inbox external off`;
CI is green on `$MERGE_SHA`. Then apply §10.3 (the deploy procedure's lease guard, run
windows and modes check) to `deploy_aliyun.sh`, and deploy:

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

If the shadow bundle is ever removed, drop `solvely-wiki-shadow` from `idle` and `$SHADOW`
from `codex_jobs`.

Once, the header files (the owner token comes from the password manager; a header file keeps
tokens out of `ps`):

```bash
install -d -m 0700 $FS
( umask 077; read -rs OWNER; printf 'Authorization: Bearer %s\n' "$OWNER" > $FS/owner.h
  printf 'Authorization: Bearer %s\n' "$(legacy_token)" > $FS/legacy.h )
```

Checks, each printing what its comment says. Never print `/etc/environment` itself: a
commented line in it holds a model gateway token.

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
# H9. The watchdog has somewhere to page (step 10).
grep -c '^AIWIKI_WATCHDOG_FEISHU_WEBHOOK=.' /etc/ai-wiki-watchdog.env        # 1, else add the webhook first
# H10. The rest of what step 11 removes (names only).
grep -oE '^[# ]*[A-Za-z_]+=' /etc/environment         # PATH= and the commented #ANTHROPIC_BASE_URL= #ANTHROPIC_AUTH_TOKEN=
systemctl cat ai-wiki-worker.service | grep -E '^(# /|EnvironmentFile=)'   # record where each EnvironmentFile= comes from
ls -d /home/admin/.lark*                             # admin's lark-cli login directory: record it as LARK for step 11
```

If any check fails, stop. Then back up what later steps change:

```bash
cp -a $DROPIN $FS/worker.service.d.before
cp -a /etc/systemd/system/ai-wiki-watchdog.service /etc/systemd/system/ai-wiki-watchdog.service.d $FS/
cp -a /etc/systemd/system/ai-wiki-watchdog.timer $FS/
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

These files are the only copy of the legacy agent instructions, autopilot prompt and skill
contents, and §13 needs them even after step 11. Keep them, plus a copy in the password
manager's secure notes or another private store, until the Codex code is deleted (design
W19, §12).

### Members

Send the members the change for day 0: `ai-wiki ingest` works as before, but a submission is
curated by the maintainer's next daily run (04:00 CST), so within about a day, instead of
within minutes; `ai-wiki jobs <id>` follows it. Nothing to reinstall.

## 2. The Auditor and the isolation gate (day −1, no schedule)

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
the maintainer's (`claude-opus-5-5-combos`). `AUDITOR_RUNTIME` is the full id of the runtime
chosen under "Where and when" (`multica runtime list --output json | jq -r '.[] | select(.id |
startswith("<its prefix>")) | .id'`).

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
brings it). `config set --endpoint` keeps a token saved earlier, so check that user's saved
token next (issue A1 shows it too); remove any but a `member:` one with
`( umask 077; jq 'del(.token)' ~/.ai-wiki/config.json > ~/.ai-wiki/config.json.new && mv ~/.ai-wiki/config.json.new ~/.ai-wiki/config.json )`.

**Issue R1** (runtime, `$PROD_AGENT`), title `[OPS] AI Wiki final cut-over R1: pin the CLI,
isolation`. Text (`$FL/issue-R1.md`, with `<MERGE_SHA>` and `<AUDITOR_AGENT>` filled in):

```text
One-time operations task from the owner, not a maintenance run. In one bash shell on this host,
run exactly the commands below, in order, and nothing else: no maintain, ingest or audit, no
other installs, no edits to any file. Post one comment with every command and its complete
output verbatim, then set this issue to done with --no-start, or to blocked if one of the first
six commands failed (the last two report; a nonzero exit there is expected and not a failure).

id -un; hostname
find "$(uv tool dir)/ai-wiki" -path '*ai_wiki-*.dist-info/direct_url.json' -exec cat {} \;
uv tool install --force "git+https://github.com/Scorpion1221/ai-wiki@<MERGE_SHA>" && hash -r
ai-wiki -b solvely-wiki health --json
ai-wiki -b solvely-wiki doctor --role curator --json
ai-wiki maint end --help
env -u AIWIKI_TOKEN ai-wiki -b solvely-wiki doctor --role member --json
multica agent env get <AUDITOR_AGENT> >/dev/null 2>"${TMPDIR:-/tmp}/env-get.err"; echo "env get exit $?"; head -c 300 "${TMPDIR:-/tmp}/env-get.err"
```

**Issue A1** (auditor host, `$AUDITOR_AGENT`), title `[OPS] AI Wiki final cut-over A1:
auditor preflight, isolation`:

```text
One-time operations task from the owner, not an audit run. Run exactly the commands below and
nothing else; post one comment with every output verbatim; set this issue to done with
--no-start, or to blocked if one of the first three commands failed (the last two report; a
nonzero exit there is expected and not a failure).

id -un; hostname
ai-wiki -b solvely-wiki doctor --role auditor --json
ai-wiki -b solvely-wiki health --json
env -u AIWIKI_TOKEN ai-wiki -b solvely-wiki doctor --role member --json
multica agent env get 1dcccd34-e9e4-48c7-a0a3-32c061d4c284 >/dev/null 2>"${TMPDIR:-/tmp}/env-get.err"; echo "env get exit $?"; head -c 300 "${TMPDIR:-/tmp}/env-get.err"
```

Neither issue prints a token: `env get` discards its output, and `doctor` shows principals and
scopes only.

Verify from R1: the first `find` line's `commit_id` is `PREV_CLI` (write it down); `health`
shows `compatible: true` and the merged `client_version`; `doctor --role curator` shows `"ok":
true`; `maint end --help` lists `--format`. Every agent on `df0fb673` shares this CLI; the
legacy flow keeps working with it (its `maintain`, `audit` and `jobs` verbs are unchanged), so
the 04:00 run before step 3 is the check. From A1: `doctor --role auditor` `"ok": true`
(scopes exactly read and audit; `git`, `uv` present), `health` `compatible: true`. Until step
6 audit changesets are refused (`AIWIKI_AUDIT=codex`), so the agent can do nothing else yet.

**The isolation gate** (docs/external-agents.md §1 and §3), from both comments:

1. The `id -un` and `hostname` pairs differ: the same host is acceptable under another user,
   the same user on the same host is not.
2. Both `env get` lines exit nonzero with a permission error (not `command not found` or a
   network error): neither host's Multica account can read the other role's token.
3. Each `doctor --role member` without `AIWIKI_TOKEN` fails its `config` check (no token
   saved) or names a `member:` principal in its `scopes` detail. On the runtime host that is
   `member:legacy-token`, which holds every scope until step 7 narrows it; step 7 runs before
   the maintainer's first run. A `human:` or `process:` principal saved there must go before
   step 8: a one-time issue runs the `jq 'del(.token)'` line above on that host (agents there
   that read the wiki then need a member token of their own).

If 1 or 2 fails, stop: the server cannot tell a stolen token from its owner, so a maintainer
that reaches the auditor's token (or the reverse) audits its own work. Fix it as
docs/external-agents.md §3 says (another OS user or host; a Multica member account that is
not a workspace owner or admin for the runtime's daemon; or the token outside Multica), or go
on only with the owner's written acceptance of that risk, recorded with this runbook's copy.

Rollback: `multica autopilot delete $AUDITOR_AP`, `multica agent archive $AUDITOR_AGENT`,
`multica skill delete $AUDITOR_SKILL`, and on the host `pp remove process:ai-wiki-auditor;
pp check; hup`. R1's CLI: the same issue with
`uv tool install --force "git+https://github.com/Scorpion1221/ai-wiki@<PREV_CLI>" && hash -r`.

## 3. Pause the legacy and shadow runs (day 0, after the 04:00 run)

Laptop, once the 04:00 CST run's issue is done:

```bash
multica autopilot runs $PROD_AP --limit 1 --output json | jq -c '.runs[0] | {status, issue_id, created_at}'   # completed
multica autopilot update $PROD_AP --status paused --output json | jq -r '.status // .autopilot.status'     # paused
multica autopilot update $SHADOW_AP --status paused --output json | jq -r '.status // .autopilot.status'   # paused
```

The shadow stops so that no shadow run holds a lease during this sitting's restarts; step 9
retires it after the canary. If its 05:30 run already started, wait until its issue is done.
Host: the shadow's Codex audit timer stops too (from step 6 on, an external audit mode refuses
its requests):

```bash
systemctl disable --now ai-wiki-shadow-audit.timer
systemctl is-active ai-wiki-shadow-audit.timer                                # inactive
```

The legacy run may leave a background `ai-wiki maintain` and queued Codex jobs behind: on the
host, wait until `codex_jobs` prints 0 and `idle && echo idle` prints `idle` (issue R2 also
refuses while a `maintain` still runs). Rollback: `multica autopilot update $PROD_AP --status
active`, the same for `$SHADOW_AP`, and `systemctl enable --now ai-wiki-shadow-audit.timer`.

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

Then the agent and its autopilot. `AUDITOR_AGENT` comes from step 2. The schedule stays the
daily `0 4 * * *` trigger (`d4fb8f06`), unchanged:

```bash
awk 'f;/^---$/{f=1}' docs/prompts/production-agent-instructions.md > $FL/prod-instructions.md
sed "s/<Auditor agent id>/$AUDITOR_AGENT/" docs/prompts/production-autopilot-prompt.md > $FL/prod-prompt.md
grep -c '<Auditor agent id>' $FL/prod-prompt.md                                   # 0
multica agent skills set $PROD_AGENT --skill-ids $CURATING_SKILL,$OKF_SKILL
multica agent update $PROD_AGENT --instructions "$(cat $FL/prod-instructions.md)" --max-concurrent-tasks 1
multica autopilot update $PROD_AP --title "AI Wiki maintainer" \
  --issue-title-template "[AUTO] AI Wiki sync {{date}}" --description "$(cat $FL/prod-prompt.md)"
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
#   paused, true, ["0 4 * * *"]
```

Rollback (the archive of step 1):

```bash
cd $FL
multica agent skills set $PROD_AGENT --skill-ids $AIWIKI_SKILL,$LEGACY_SKILL
multica agent update $PROD_AGENT --instructions "$(cat prod-instructions.legacy.md)" --max-concurrent-tasks 2
multica autopilot update $PROD_AP --title "AI Wiki daily incremental sync" \
  --issue-title-template "[AUTO] AI Wiki daily sync {{date}}" --description "$(cat autopilot-prompt.legacy.md)"
```

The skill contents roll back from `skill-<id>.before.json` as in the Phase 2 runbook §11.3.

## 6. Writer drop-in: the final flags (host)

One drop-in holds every final flag, `AIWIKI_LLM=off` included, so the canary runs the final
state and a rollback removes one file. Under `off` a queued Codex job would stay queued, hence
the `codex_jobs` guard. The drop-in sorts after `phase2-shadow.conf`, which it overrides:

```bash
[ "$(codex_jobs)" = 0 ] || echo 'STOP: a Codex job is queued or running; wait (step 3)'
EPOCH=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "$EPOCH" > $FS/backlog-epoch
cat > $DROPIN/phase3-final.conf <<EOF
# Final state (docs/final-cutover-runbook.md step 6). Sorts after phase2-shadow.conf.
[Service]
Environment=AIWIKI_CHANGESETS_COMMIT=solvely-wiki,solvely-wiki-shadow
Environment=AIWIKI_INTAKE=inbox
Environment=AIWIKI_AUDIT=external
Environment=AIWIKI_LLM=off
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
#    "codex_audit_manual":[],"llm":"off"}
curl -s -H @$FS/owner.h 'http://127.0.0.1:8788/health?bundle=solvely-wiki' | jq -c '{bundle, concepts, build, writer_agent}'
#   writer_agent {"runtime":"off"}
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H @$FS/owner.h \
  'http://127.0.0.1:8788/jobs/no-such-job/audit?bundle=solvely-wiki'                # 409: audit is external
curl -s -H @$FS/owner.h 'http://127.0.0.1:8788/audit/backlog?bundle=solvely-wiki&limit=5' | jq -c 'keys'   # the backlog answers
systemd-cgls --no-pager -u ai-wiki-worker.service | grep -c codex                    # 0: nothing but uv and python
journalctl -u ai-wiki-worker --since -10min --no-pager | grep -iE 'traceback|error' || echo clean
```

Rollback, when idle, and only while the Codex configuration is on the host (after step 11,
first its rollback): `rm $DROPIN/phase3-final.conf && systemctl daemon-reload && restart_idle`;
`/whoami` shows today's modes again (compare with `$FS/modes.before.json`). Member items that
arrived meanwhile go back to Codex with W14's `POST /admin/inbox/requeue` (owner token, see
its route docstring for the body); changesets already committed are valid OKF content and stay.

## 7. Members: the legacy token to read and submit (host)

`member:legacy-token` (the shared `eb17…`) still holds every scope on `solvely-wiki`. In the
final state members only read and submit, and before the maintainer's first run: a runtime
host may keep this token saved (step 2), where an agent that unsets its own `AIWIKI_TOKEN`
would reach it.

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

## 8. Canary: one item, one auditor run, then the schedules

**8a. The maintainer, one item** (laptop). The production prompt with `max_items=1`, as a
one-time issue for the production agent:

```bash
cd $FL && sed 's/^max_items=6 /max_items=1 /' prod-prompt.md > canary-prompt.md
grep -c '^max_items=1 ' canary-prompt.md                                                # 1
multica issue create --title "[CANARY] AI Wiki sync, 1 item" --assignee-id $PROD_AGENT \
  --description-file canary-prompt.md --output json | jq -r '.id // .issue.id'          # CANARY_ISSUE
multica issue metadata set $CANARY_ISSUE --key ai_wiki_ops --value true
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
changeset: run 8a once more. On the host, a replay of the watchdog shows no alert:
`as_admin /home/admin/app/.venv/bin/python /usr/local/bin/ai-wiki-watchdog --bundle $PROD --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | jq -c '{status, alerts: [.alerts[].key]}'`.

**8b. One auditor run** (laptop): `multica autopilot trigger $AUDITOR_AP`. When its issue is
done, the canary's concept was reviewed:

```bash
uv run ai-wiki -b solvely-wiki cat <the concept_file> --json | jq -c '.metadata | {status, trust, verification_current}'
#   stable; machine-confirmed and true (verified or a corrected narrowing), or unverified with the reviewer's note
git -C $ws/clone pull -q && git -C $ws/clone log -3 --format='%h %s'                   # the audit changeset's commit
grep -A3 '^verified:' $ws/clone/<the concept_file>                                      # by: process:ai-wiki-auditor, never the maintainer
```

**8c. The schedules** (laptop). The maintainer's daily 04:00 trigger resumes; the Auditor
takes the cron the header table of `docs/prompts/auditor-agent-instructions.md` names (today
`0 7,15 * * *`):

```bash
multica autopilot update $PROD_AP --status active --output json | jq -r '.status // .autopilot.status'   # active
multica autopilot trigger-add $AUDITOR_AP --kind schedule --cron "0 7,15 * * *" \
  --timezone Asia/Shanghai --label "07:00/15:00 Asia/Shanghai"
multica autopilot get $PROD_AP --output json | jq -c '[.triggers[] | {cron_expression, next_run_at}]'
```

Stop and roll back (§13) at once if the gate is bypassed (a curator's changeset carries a
`verified` event), a receipt disagrees with origin, the fresh clone fails `okf-validate`, two
scheduled runs in a row leave the cursors where they were, or the owner finds two serious
errors in one run. After each daily run of the first week: the issue's first comment line,
`ai-wiki -b solvely-wiki maint status --json`, and the watchdog's 07:00 result.

## 9. Retire the shadow agent (after the canary)

Laptop (its autopilot has been paused since step 3):

```bash
multica agent archive $SHADOW_AGENT --output json | jq -r '.archived_at // .agent.archived_at'   # a timestamp
```

Host: the shadow's Codex audit timer, its principal, and the watchdog's shadow bundle go (the
shadow's cursors and commits stop moving, so its checks would page):

```bash
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
To remove it instead, run the Phase 2 runbook's §12 R2 and R4 blocks, with `solvely-wiki`
alone in `AIWIKI_CHANGESETS_COMMIT` (edit `phase3-final.conf`, then `restart_idle`).

Rollback: `multica agent restore $SHADOW_AGENT`, and `mv $FS/watchdog-phase2-shadow.conf
/etc/systemd/system/ai-wiki-watchdog.service.d/phase2-shadow.conf` and `systemctl
daemon-reload`. The Codex audit timer and its principal come back only with §13 (an external
audit mode refuses their requests): as the Phase 2 runbook §9 installs them, from the files in
`$FS`, with a new token (`pp add auditor --id process:ai-wiki-shadow-audit --bundle
solvely-wiki-shadow`).

## 10. Watchdog and deploy procedure

The watchdog runs once a day, at 07:00 CST after the 04:00 maintainer run, on the writer
(`--bundle`) and on the runtime host (`--multica --no-checkpoint`), as
docs/maintenance-watchdog.md "Final state" describes.

### 10.1 Writer side (host)

The deploy put the merged tree in `/home/admin/app`. If the script changed, back up
`/usr/local/bin/ai-wiki-watchdog` and install the merged one, as in the Phase 2 runbook plan
step 4; then move the timer to once a day:

```bash
cmp -s /home/admin/app/scripts/maintenance_watchdog.py /usr/local/bin/ai-wiki-watchdog || echo changed
cp -a /usr/local/bin/ai-wiki-watchdog $FS/ai-wiki-watchdog.before
install -m 0755 -o root -g root /home/admin/app/scripts/maintenance_watchdog.py /usr/local/bin/ai-wiki-watchdog
install -d -m 0755 /etc/systemd/system/ai-wiki-watchdog.timer.d
cat > /etc/systemd/system/ai-wiki-watchdog.timer.d/daily.conf <<'EOF'
# Final state: once a day, after the 04:00 maintainer run (docs/final-cutover-runbook.md step 10).
[Timer]
OnCalendar=
OnCalendar=*-*-* 07:00:00 Asia/Shanghai
EOF
systemctl daemon-reload && systemctl restart ai-wiki-watchdog.timer
systemctl list-timers --no-pager ai-wiki-watchdog.timer                                    # NEXT: 07:00 CST
systemctl show ai-wiki-watchdog -p ExecStart --value | grep -o -- '--bundle [^ ]*'       # --bundle /home/admin/solvely-wiki only
as_admin /home/admin/app/.venv/bin/python /usr/local/bin/ai-wiki-watchdog --bundle $PROD \
  --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | jq -c '{status, cursors: .checks["maint:solvely-wiki"].cursors, alerts: [.alerts[].key]}'
#   ok; repos and issues with their ages; no alert
```

What the writer side pages on: a member's submission waiting in ready over 24 h (it missed a
daily run), any item over 72 h or needing a human, a cursor that has not advanced for 30 h, a
run lease stuck over 3 h, no commit for 48 h, and writer job failures. A missed or failed
daily run pages the same morning from the Multica side (10.2). The Auditor's backlog age has
no check yet; its failed or missing runs page from 10.2.

Rollback: `rm -r /etc/systemd/system/ai-wiki-watchdog.timer.d && systemctl daemon-reload &&
systemctl restart ai-wiki-watchdog.timer`, and `install -m 0755 $FS/ai-wiki-watchdog.before
/usr/local/bin/ai-wiki-watchdog`.

### 10.2 Multica side (runtime host, issue R3)

The Multica side reads the maintainer's and the Auditor's autopilot runs and issues with the
runtime host's `multica` login, and pages the same Feishu group. Its webhook reaches that host
through the production agent's custom env for one run (laptop):

```bash
read -rs HOOK && read -rs HOOK_SECRET && export HOOK HOOK_SECRET   # the webhook, and its signing secret or empty
multica agent env get $PROD_AGENT | jq -c '.custom_env | map_values("****")
  + {AIWIKI_WATCHDOG_FEISHU_WEBHOOK: $ENV.HOOK, AIWIKI_WATCHDOG_FEISHU_SECRET: $ENV.HOOK_SECRET}' \
  | multica agent env set $PROD_AGENT --custom-env-stdin >/dev/null
unset HOOK HOOK_SECRET
```

Issue R3 (`$PROD_AGENT`), title `[OPS] AI Wiki final cut-over R3: Multica-side watchdog`,
with `<MERGE_SHA>` and `<AUDITOR_AP>` filled in:

```text
One-time operations task from the owner, not a maintenance run: install the daily Multica-side
watchdog for this user. In one bash shell run exactly the commands below, in order, and nothing
else; stop at the first failure. Never print the webhook. Post one comment with every command
and its complete output verbatim, then set this issue to done with --no-start, or to blocked if
you stopped.

case "$(date +%z)" in +0800) H=7 ;; +0000) H=23 ;; *) echo "STOP: zone $(date +%z)"; false ;; esac && echo "hour $H"
test -n "$AIWIKI_WATCHDOG_FEISHU_WEBHOOK" && echo webhook-present
PY="$(uv tool dir)/ai-wiki/bin/python"; M="$(command -v multica)"; W="$HOME/.local/bin/ai-wiki-watchdog"; E="$HOME/.config/ai-wiki-watchdog/env"; S="$HOME/.local/state/ai-wiki-watchdog"; echo "$PY $M"
install -d -m 0700 "$HOME/.config/ai-wiki-watchdog" "$S"
( umask 077; printf 'AIWIKI_WATCHDOG_FEISHU_WEBHOOK=%s\nAIWIKI_WATCHDOG_FEISHU_SECRET=%s\n' "$AIWIKI_WATCHDOG_FEISHU_WEBHOOK" "$AIWIKI_WATCHDOG_FEISHU_SECRET" > "$E" )
curl -fsSL "https://raw.githubusercontent.com/Scorpion1221/ai-wiki/<MERGE_SHA>/scripts/maintenance_watchdog.py" -o "$W" && chmod 0755 "$W"
env -u AIWIKI_WATCHDOG_FEISHU_WEBHOOK -u AIWIKI_WATCHDOG_FEISHU_SECRET "$PY" "$W" --multica --no-checkpoint --multica-bin "$M" --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | head -c 1500
( crontab -l 2>/dev/null | grep -v ai-wiki-watchdog; echo "0 $H * * * set -a; . $E; set +a; $PY $W --multica --no-checkpoint --multica-bin $M --state-file $S/maintainer.json --label multica-maintainer >> $S/cron.log 2>&1"; echo "5 $H * * * set -a; . $E; set +a; $PY $W --multica --no-checkpoint --multica-bin $M --autopilot-id <AUDITOR_AP> --state-file $S/auditor.json --label multica-auditor >> $S/cron.log 2>&1" ) | crontab -
crontab -l | grep -c ai-wiki-watchdog
```

Verify from the comment: `hour 7` (or `hour 23` on a UTC host: 07:00 CST), `webhook-present`,
the replay's `"status"` is `ok` or names only real alerts with `"checkpoint": null`, and the
crontab count is `2`. Then take the webhook back out of the agent's env (laptop):

```bash
multica agent env get $PROD_AGENT | jq -c '.custom_env | del(.AIWIKI_WATCHDOG_FEISHU_WEBHOOK, .AIWIKI_WATCHDOG_FEISHU_SECRET)
  | map_values("****")' | multica agent env set $PROD_AGENT --custom-env-stdin >/dev/null
multica agent env get $PROD_AGENT | jq -c '.custom_env | keys'                    # ["AIWIKI_TOKEN"], no webhook keys
```

The next morning, `$S/cron.log` on that host (a later ops issue can `tail` it) and the Feishu
group show the first runs. Rollback: an ops issue running `crontab -l | grep -v
ai-wiki-watchdog | crontab -` and `rm -r ~/.config/ai-wiki-watchdog`.

### 10.3 Deploy procedure (laptop, `deploy_aliyun.sh`)

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

2. **Run windows**: besides the existing 03:30–06:30 CST rule (the maintainer's 04:00 run),
   refuse from 30 minutes before to 30 minutes after each Auditor start (its cron, today
   07:00 and 15:00 CST).
3. **After the deploy**, compare the writer's `/whoami` `.modes` with the ones before it,
   read with the token the script already uses for its health checks: the flags live in
   drop-ins, not the app directory, so a deploy must not change them. Compare only the keys
   the old build reported: a key the new build adds is new, not changed (step 0's deploy adds
   `llm`, whose value step 0's own check reads):

   ```bash
   jq -n --argjson a "$BEFORE" --argjson b "$AFTER" \
     '($a | keys) as $k | ($b | with_entries(select(.key as $x | $k | index($x)))) == $a' | grep -qx true \
     || { echo 'modes changed by the deploy'; exit 1; }
   ```

4. Mirror: unchanged (it keeps the live container's mounts, Phase 2 runbook §6).

Rollback: `deploy_aliyun.sh.pre-final`.

## 11. Remove the Codex config, the gateway credential and the lark-cli login (host)

Right after steps 8 to 10, in the same sitting: with `AIWIKI_LLM=off` the writer reads none of
this, and the backups below keep a rollback possible for 30 days (design §8.4 and §9 phase 5).
`/home/admin/.codex` stays: it is admin's own interactive Codex login (a `codex` session and
the `codex-auth` daemon use it), not the writer's. `LARK` is the directory H10 listed.

```bash
whoami_w | jq -r .modes.llm                                                   # off, else stop: step 6 first
grep -rlsE 'codex-gateway|codex-9router' /home/admin/.bashrc /home/admin/.profile /home/admin/.codex/config.toml /etc/environment \
  && echo 'STOP: something else uses the 9Router wrapper or its secret; keep them' || echo unused-elsewhere
grep -rlsE 'lark-cli' /etc/cron* /var/spool/cron /etc/systemd/system /home/admin/.bashrc /home/admin/.profile \
  && echo 'STOP: a job runs lark-cli; keep its login' || echo lark-unused
LARK=<the directory H10 listed>; test -d "$LARK" && echo "$LARK"
```

Only after `off`, `unused-elsewhere` and `lark-unused`:

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ); BK=$FS/codex-host-$TS; install -d -m 0700 $BK
cp -a $DROPIN $BK/worker.service.d && cp -a /etc/environment $BK/environment
tar -C /home/admin -czpf $BK/admin-codex.tgz .ai-wiki/config.json .local/bin/codex-9router \
    .config/secrets/codex-gateway.env "${LARK#/home/admin/}"
chmod 600 $BK/admin-codex.tgz $BK/environment && tar -tzf $BK/admin-codex.tgz | head   # the three files and the lark-cli directory
mv $DROPIN/codex-runtime.conf $DROPIN/zz-agent-config.conf $BK/ && systemctl daemon-reload
systemctl cat ai-wiki-worker.service | grep -E '^EnvironmentFile='          # EnvironmentFile=/etc/environment only, else STOP
cat > $DROPIN/zz-no-etc-environment.conf <<'EOF'
# The writer loads no host-wide environment (docs/final-cutover-runbook.md step 11): /etc/environment
# held a model gateway credential. The unit sets PATH and HOME itself.
[Service]
EnvironmentFile=
EOF
sed -i '/^[[:space:]]*#\?[[:space:]]*ANTHROPIC_/d' /etc/environment
systemctl daemon-reload
as_admin shred -u /home/admin/.config/secrets/codex-gateway.env
as_admin rm /home/admin/.local/bin/codex-9router /home/admin/.ai-wiki/config.json
as_admin rm -r "$LARK"
as_admin env -i HOME=/home/admin PATH=/usr/local/bin:/usr/bin:/bin git -C $PROD fetch --dry-run origin && echo git-ok
restart_idle
```

`git-ok` shows the writer's Git and SSH work with the unit's own `PATH`, which
`/etc/environment` no longer overrides. Verify:

```bash
ls $DROPIN                  # okf-v02.conf phase1-principals.conf phase2-shadow.conf phase3-final.conf zz-no-etc-environment.conf
systemctl show ai-wiki-worker -p EnvironmentFiles --value                               # empty
systemctl show ai-wiki-worker -p Environment --value | tr ' ' '\n' | grep -cE '^AIWIKI_(AGENT_|CONFIG=)'   # 0
grep -c ANTHROPIC /etc/environment                                                     # 0
whoami_w | jq -r .modes.llm                                                            # off
curl -s -H @$FS/owner.h 'http://127.0.0.1:8788/health?bundle=solvely-wiki' | jq -c .writer_agent   # {"runtime":"off"}
for f in /home/admin/.config/secrets/codex-gateway.env /home/admin/.local/bin/codex-9router "$LARK"; do
  test ! -e "$f" || echo "still there: $f"; done; echo checked
```

The gateway token in `/etc/environment` was world-readable (and a review's transcript saw it),
and the backup holds a live 9Router key: the owner revokes both at their gateways now, and
the lark-cli app login in the Feishu console. Keep `$BK` root-only for 30 days, then `shred -u
$BK/admin-codex.tgz $BK/environment`. A rollback after the revocations needs new credentials.
Rollback:

```bash
tar -C /home/admin -xzpf $BK/admin-codex.tgz && cp -a $BK/environment /etc/environment
rm $DROPIN/zz-no-etc-environment.conf
mv $BK/codex-runtime.conf $BK/zz-agent-config.conf $DROPIN/ && systemctl daemon-reload
```

and then step 6's rollback (a writer with `AIWIKI_LLM=off` reads none of it).

Finally, `shred -u $FS/owner.h $FS/legacy.h` on the host, and `unset AIWIKI_TOKEN` on the laptop.

## 12. What stays, what goes

- Stays: the gate (`runtime/changeset.py`, `policy.py`, the `_transaction` pipeline),
  recovery, receipts, the collectors, the shadow bundle as the canary target.
- Rollback-only until the Codex code is deleted (design W19): the legacy Codex runner, `POST
  /jobs/{id}/audit`, `/jobs/pending-audit`, `ai-wiki maintain` and `audit`, the
  `ai-wiki-maintainer` skill, the archive in `$FL` (the only copy of the legacy texts), the
  legacy ledger on the runtime host (`~/.local/state/ai-wiki-maintainer/solvely-wiki`,
  read-only from step 4 on).
- Docs: `docs/prompts/shadow-*.md` record the Phase 2 shadow agent; a canary on the shadow runs
  the production texts instead (docs/external-agents.md §6).

## 13. Rollback to the legacy path

Each step's own rollback is listed with it. To go back to the legacy flow from any point
after step 5, in this order (skip what never ran):

1. **Stop the new runs** (laptop): `multica autopilot update $PROD_AP --status paused` and
   `multica autopilot update $AUDITOR_AP --status paused`. Wait until the host's `idle` passes.
2. **The writer back to Codex** (host): step 11's rollback if it ran, then
   `rm -f $DROPIN/phase3-final.conf && systemctl daemon-reload && restart_idle`.
   `whoami_w | jq -c .modes` equals `$FS/modes.before.json`. Hand member items back to Codex
   with `POST /admin/inbox/requeue` (step 6's rollback), and widen the legacy token again
   (step 7's rollback).
3. **The agent back to the legacy flow** (laptop): step 5's rollback block.
4. **The cursors and unfinished items back to the ledger** (issue RB1 below): the writer's
   cursors as a v4 checkpoint on the newest run issue, so the legacy `find` picks them up, and
   the unfinished work items as a `maintain` manifest.
5. **Resume the legacy schedule** (laptop): `multica autopilot update $PROD_AP --status active`;
   its next run starts from RB1's checkpoint.
6. **Archive the queue** (host, once `status_w solvely-wiki | jq .leases` shows no live
   lease), so the watchdog stops paging on cursors nothing advances:
   `as_admin mv $PROD/.okf/maint $PROD/.okf/maint.rolled-back-$(date +%F)`.
7. **The watchdog**: the writer side stays as it is. On the runtime host R3's rollback removes
   the Multica side, which pages on the legacy runs only once reinstalled without
   `--no-checkpoint` (docs/maintenance-watchdog.md, "Multica runtime host").
8. The Auditor stays paused (`multica agent archive $AUDITOR_AGENT` to retire it); committed
   changesets and audit changesets are valid OKF content and stay.

Issue RB1, title `[OPS] AI Wiki rollback RB1: export cursors to the legacy ledger`, assigned
to `$PROD_AGENT` after step 3 of this list restored its legacy skills, and marked
`ai_wiki_ops` like every one-time issue; `<LAST_RUN_ISSUE>` is
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
- Step 2's isolation gate may fail on the Multica check: a runtime whose daemon is logged in
  as a workspace owner or admin can read every agent's custom env. The fix changes who the
  daemon runs as, which the owner decides.
- One maintainer run a day with `max_items=6` curates at most 6 items a day. The import of
  step 4 and a busy day can queue more; `maint_ready_stale` (72 h) and the member check (24 h)
  say when to raise `max_items`.
- The first days of `AIWIKI_AUDIT=external` release the old unverified concepts into the
  backlog a few a day (the seed), so the Auditor's backlog stays long for a while; the
  maintainer is not affected. The backlog's age has no watchdog check yet.
- The writer still runs as `admin`, the user of an interactive Codex login on the same host.
  `AIWIKI_LLM=off` guarantees the service never starts an agent; moving the writer to its own
  user (design §8.4) is the host hardening that remains.
