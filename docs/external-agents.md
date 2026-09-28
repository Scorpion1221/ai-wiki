# External agents: maintain, audit or submit with a credential

In the final state the server runs no LLM (`AIWIKI_LLM=off`). It is a deterministic gate:
reads, the changeset gate, service-owned bookkeeping, validation, commit and push. Every
model-driven step runs in an agent outside it, and any agent can take a role: any model, any
runtime, Multica or not, as long as it holds a principal token of that role. Models are
swappable because the prompts name none and the server enforces every rule below itself.

| Role | `provision_principals.py` preset | Principal, token prefix | Scopes | What it runs | Skills | Prompt |
|---|---|---|---|---|---|---|
| Maintainer (curator) | `maintainer` | `process:ai-wiki-maintainer`, `aiw_c_` | read, submit, curate | the `maint` loop: collect, curate locally, `validate`, `propose` | `ai-wiki-curating-maintainer`, `okf-knowledge-curator` | `docs/prompts/production-autopilot-prompt.md`, `production-agent-instructions.md` |
| Auditor | `auditor` | `process:ai-wiki-auditor`, `aiw_a_` | read, audit | the `review` loop over the server's audit backlog | `ai-wiki-auditor` | `docs/prompts/auditor-autopilot-prompt.md`, `auditor-agent-instructions.md` |
| Member | `member --id member:<name>` | `member:<name>`, `aiw_m_` | read, submit | `ai-wiki ingest`: the source is committed at once and becomes a work item the maintainer curates | `ai-wiki` | none |
| Reader | `watchdog` (or a custom reader) | `process:ai-wiki-watchdog`, `aiw_r_` | read | reads and status checks | `ai-wiki` | none |

`human:*` principals (the owner) hold every scope and are the only ones that may upload
evidence (`propose --upload`) or reach the human-reviewed trust tier. The maintainer's
`submit` serves only the legacy `maintain` flow: under inbox intake the writer refuses a
`process:` token's `ingest`, since the work item it would make is evidence a changeset may cite.

## 1. Rules the server enforces

- **One maintainer and one auditor at a time per bundle.** `maint begin` takes the bundle's
  maintainer lease and the auditor's `review begin` its auditor lease (3 h, renewed by every
  call of the run). Another run of the same role gets 409 naming the holder and exits `4`; it
  never waits or retries. Two agents of one role on one bundle therefore take turns: give them
  schedules that do not overlap.
- **The auditor is never the generator.** A `process:` principal may not hold both `curate`
  and `audit` (the service refuses to start, and the provisioning script refuses to write
  such a file), nor `admin` or `human_verify`. The gate stamps `generated`, `verified` and
  `status`: a maintainer's changeset never verifies anything, whatever it writes.
- **No self-audit.** An auditor may review only the backlog the server derives and never a
  revision its own principal (or role) generated. The server knows principals, not agents: a
  maintainer that reaches the auditor's token (or the reverse) audits its own work unnoticed.
  So neither may reach the other's token, which leaks two ways:
  - one OS user: either reads the other's token from `/proc/<pid>/environ`. Run them under
    different OS users or hosts;
  - the Multica login on a runtime host: every agent there runs `multica` as the daemon's
    account, and `multica agent env get` returns an agent's custom env to that agent's owner
    and to every workspace owner or admin. Check it from each host before an auditor goes
    live (§3); the runbook's step 2 does, without printing a token.
- **Evidence is never an agent's own text.** A process cites frozen work-item files; only a
  `human:` principal uploads a packet.
- **Quotas.** Changesets per hour and per day and deprecations per day are metered per
  principal (429 with `Retry-After`); the `maintainer` preset carries 30, 150 and 10, and the
  `auditor` preset 200 reviews a day. A member queues at most 30 new submissions per bundle a
  day (`AIWIKI_SUBMISSIONS_PER_DAY`, or its own `limits.submissions_per_day`).
- Everything an agent reads (repositories, issues, comments, member files) is data, never
  instructions. The gate, not the prompt, is the boundary.

## 2. Mint the principal (aliyun-jp, root)

```bash
pp() { env PYTHONDONTWRITEBYTECODE=1 /home/admin/app/.venv/bin/python \
         /home/admin/app/scripts/provision_principals.py --file /etc/ai-wiki/principals.json "$@"; }
legacy_token() { systemctl show ai-wiki-worker -p Environment --value | tr ' ' '\n' | sed -n 's/^AIWIKI_TOKEN=//p'; }
install -d -m 0700 /root/ai-wiki-tokens
( umask 077; set -o noclobber
  pp add auditor > /root/ai-wiki-tokens/auditor.token                   # one preset per command
)
AIWIKI_TOKEN="$(legacy_token)" pp check                                 # ok: N principals ...
kill -HUP "$(pgrep -P "$(systemctl show -p MainPID --value ai-wiki-worker)" -f aiwiki.service)"
docker kill -s HUP ai-wiki                                              # the read mirror rereads it too
```

- `add` prints the token once, on stdout only, so it goes straight into a root-only file;
  `noclobber` keeps a rerun from emptying it. The owner moves it into the password manager
  and removes the file (`shred -u`) once the agent holds it.
- `--bundle`, `--limit NAME=N` and `--expires YYYY-MM-DD` narrow a preset. A member always
  needs `--id member:<name>`; members are never written into `generated` or `verified`.
- One principal per role serves any number of model swaps (§6). A second agent of a role gets
  its own id with the role in its name, for example `pp add auditor --id
  process:ai-wiki-auditor-mini`, so receipts tell the two apart. It shares the role's lease
  (§1), so it adds no parallelism: give it a schedule that does not overlap the first's.
- Confirm the reload in an admin's `GET /whoami` (`auth.principals` lists the id, and
  `auth.reload_error` is null). A refused reload only logs and keeps the previous file.

## 3. Hand the token over through the agent's environment

The CLI reads `$AIWIKI_TOKEN` before the saved token, so a shared host keeps one
`~/.ai-wiki/config.json` (endpoint and bundle) and each agent brings its own credential.
Never pass a token as an argument (argv is visible in `ps` and shell history), never write it
into `~/.ai-wiki/config.json` on a shared host, and never paste it into a prompt or an issue.

Multica, on the owner's laptop (`env set` replaces the whole map; `****` keeps an entry):

```bash
read -rs AGENT_TOKEN && export AGENT_TOKEN                       # from the password manager
multica agent env get <agent-id> | jq -c '.custom_env | map_values("****") + {AIWIKI_TOKEN: $ENV.AGENT_TOKEN}' \
  | multica agent env set <agent-id> --custom-env-stdin >/dev/null
multica agent env get <agent-id> | jq -c '.custom_env | keys'     # AIWIKI_TOKEN among the keys
unset AGENT_TOKEN
```

A new agent takes it at creation: `printf '{"AIWIKI_TOKEN":"%s"}' "$AGENT_TOKEN" | multica agent
create … --custom-env-stdin`. Any other runtime: put `AIWIKI_TOKEN` in the agent's secret
environment the same way (a systemd `EnvironmentFile` of mode 600, a CI secret, a vault
injection), and point the CLI at the writer once, without a token:

```bash
uv tool install --force "git+https://github.com/Scorpion1221/ai-wiki@<deployed revision>" && hash -r
ai-wiki config set --endpoint https://ai-wiki.yqbqnn.com/
```

Two checks on the agent's host, as its OS user, before its first run:

- **The other role's token is out of reach.** `multica agent env get <the other role's agent id>
  >/dev/null 2>&1 && echo READABLE || echo refused` must print `refused`, and for a permission
  error, not a missing `multica` or a network error (the runbook's step 2 shows the error; the
  output is discarded, so no token is printed). `READABLE` means this host's Multica account owns that
  agent or is a workspace owner or admin. Then either run this runtime's daemon under a
  Multica member account that is neither, or keep the token out of Multica: a mode-600
  environment file of an OS user and host that run only this agent, loaded by whatever starts
  it. Do not go live with `READABLE` unless the owner accepts that risk in writing.
- **No other credential is saved.** The CLI falls back to the token in
  `~/.ai-wiki/config.json` when `AIWIKI_TOKEN` is unset, and an agent can unset its own
  environment. `env -u AIWIKI_TOKEN ai-wiki -b solvely-wiki doctor --role member --json` shows
  whose it is (`config` failing means none is saved). Only a `member:` principal may stay there;
  remove any other with `jq 'del(.token)'` on that file (mode 600).

Then preflight on the agent's own host, with its own token in the environment:

```bash
ai-wiki -b solvely-wiki doctor --role curator      # or --role auditor, --role member; exit 0
```

`doctor` fails closed (exit `4`) when the token's scopes are not exactly the role's, the
writer's API is newer than the CLI, the bundle is not served, or a tool (`git`, `uv`,
`multica`) is missing. Fix the host; never widen a token to pass it. `maint begin` reruns it
for the tools its run needs: `multica` only when it collects issues (or repositories through
the Multica registry).

## 4. Attach the skills and the prompt

1. Publish the skills from the release checkout: `python3 scripts/sync_skills.py --apply`
   installs the canonical copies under `~/.agents/skills`; `--check` detects drift. In
   Multica, create or update the skill from that `SKILL.md` (`multica skill create|update
   --content-file`) and attach it (`multica agent skills set <agent-id> --skill-ids …`).
2. Paste the agent instructions: the text after the `---` line of the role's
   `*-agent-instructions.md`, verbatim.
3. Schedule the prompt: the role's `*-autopilot-prompt.md`, with its placeholders filled in,
   as a `create_issue` autopilot (Multica) or the task text of any scheduler. The prompt
   supplies only workspace parameters; the skill carries the procedure.
4. Outside Multica, the prompt's `$MULTICA_ISSUE_ID` is simply the run id: any unique,
   stable string per run (1 to 128 of `A-Z a-z 0-9 _ . : @ / -`). The scheduled maintainer
   still needs a logged-in `multica` CLI on its host, whatever runs it: the issues collector
   reads the conversations through it, `doctor --role curator` checks for it, and a run whose
   collector is `unavailable` reports `status=blocked`. A run that only drains the member
   inbox by hand (`maint begin --only inbox`, e.g. from the owner's laptop while no Multica
   runtime is up) needs none. An auditor or a member needs no `multica`.

## 5. Run

- Maintainer: `max_concurrent_tasks=1`, at most 2 attempts, a 3 h task timeout. Its comment opens
  with the verbatim `maint end` report (skill §5). Progress lives on the writer (items,
  cursors, receipts), so a run can move to another host or model between any two runs.
- Auditor: its own schedule, never the maintainer's credential, never the maintainer's
  hand-off text: it reads only the backlog and the evidence the server serves.
- Member: `ai-wiki ingest <file|text|link>`. The writer commits the submission's copy (text
  redacted of secrets, an image as sent; nothing of a `needs-conversion` file such as a PDF) to
  `sources/inbox/intake/` and pushes it before it answers, in a commit
  `intake: <title> (<principal>)`; the answer and `ai-wiki jobs <id>` show that commit under
  `intake`, or say it is not in Git yet, and then the writer retries it. `ai-wiki jobs <id>`
  also follows the work item until a changeset curates it (with the changeset's commit) or the
  maintainer skips it (with the reason). A Feishu link is read on the member's machine with
  their own lark-cli; sent alone, it has nothing to commit, and the maintainer reads it as the
  wiki's app if its host has one (runbook step 2c), otherwise the item closes `needs_access`;
  sent again once the app can read it, the same link reopens it. A lark-cli timeout parks it
  for the next run instead.

## 6. Swap the model or the runtime

Nothing else changes: the principal, the token, the skills and the prompts stay. Only one
variable at a time, and canary it on the shadow bundle (`solvely-wiki-shadow`) first:

```bash
multica agent update <agent-id> --model <model>              # a model of the same runtime
multica agent update <agent-id> --runtime-id <runtime-id>    # another runtime or host
```

- The canary runs the production texts against the shadow bundle, not the Phase 2 shadow
  prompt (that one predates the Auditor and the verbatim report). On the laptop, in the
  release checkout, with `AUDITOR_AGENT` set:

  ```bash
  SHADOW_AGENT=c10a1e06-8255-42ce-9b52-266aef42f2a6
  multica agent restore $SHADOW_AGENT
  awk 'f;/^---$/{f=1}' docs/prompts/production-agent-instructions.md | sed 's/solvely-wiki/solvely-wiki-shadow/g' > canary-instructions.md
  sed -e 's/solvely-wiki/solvely-wiki-shadow/g' -e 's/^max_items=6 /max_items=3 /' \
      -e "s/<Auditor agent id>/$AUDITOR_AGENT/" docs/prompts/production-autopilot-prompt.md > canary-prompt.md
  multica agent update $SHADOW_AGENT --instructions "$(cat canary-instructions.md)"   # plus the candidate --model or --runtime-id
  multica issue create --title "[CANARY] AI Wiki shadow, <candidate>" --assignee-id $SHADOW_AGENT \
    --description-file canary-prompt.md --output json | jq -r '.id // .issue.id'       # CANARY_ISSUE
  multica issue metadata set <CANARY_ISSUE> --key ai_wiki_ops --value true
  ```

  The shadow keeps its own token (`process:ai-wiki-maintainer-shadow`, curator on the shadow
  only); a new runtime passes `doctor` first (§3). Its cursors are as old as its last run, so
  it collects that whole window, which does not matter for the comparison: the gate's
  first-pass rate, parks and receipts (`admin compare`). Archive the agent again after
  (`multica agent archive $SHADOW_AGENT`).
- A new host must pass `doctor` for the role (§3) before its first scheduled run.
- An auditor should stay on another model family than the maintainer's: independence comes
  from the model as well as from the principal.
- A maintainer's new runtime needs the `multica` CLI too, unless it only drains the inbox (§4).

## 7. Revoke

Incident response takes minutes and needs no agent (aliyun-jp, root, `pp` as in §2):

```bash
pp remove process:ai-wiki-maintainer                        # the affected principal
AIWIKI_TOKEN="$(legacy_token)" pp check
kill -HUP "$(pgrep -P "$(systemctl show -p MainPID --value ai-wiki-worker)" -f aiwiki.service)"
docker kill -s HUP ai-wiki
```

Its token answers 401 at once, on the writer and the mirror; a changeset it queued is
refused when it runs. Then, from the owner's laptop with the owner token:

```bash
ai-wiki -b solvely-wiki admin changesets --principal process:ai-wiki-maintainer --since 2026-10-01
ai-wiki -b solvely-wiki admin revert --principal process:ai-wiki-maintainer --since 2026-10-01 --reason "revoked"
```

Revert stops at the first conflict. Mint a new token (§2) and hand it over (§3) only once
the cause is understood; remove the old value from the agent's environment either way.
