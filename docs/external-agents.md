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
| Member | `member --id member:<name>` | `member:<name>`, `aiw_m_` | read, submit | `ai-wiki ingest`: the source becomes a work item the maintainer curates | `ai-wiki` | none |
| Reader | `watchdog` (or a custom reader) | `process:ai-wiki-watchdog`, `aiw_r_` | read | reads and status checks | `ai-wiki` | none |

`human:*` principals (the owner) hold every scope and are the only ones that may upload
evidence (`propose --upload`) or reach the human-reviewed trust tier.

## 1. Rules the server enforces

- **One maintainer at a time per bundle.** `maint begin` takes the bundle's maintainer lease
  (3 h, renewed by every call of the run). Another run gets 409 naming the holder and exits
  `4`; it never waits or retries. Two maintainer agents on one bundle therefore take turns;
  give them schedules that do not overlap.
- **The auditor is never the generator.** A `process:` principal may not hold both `curate`
  and `audit` (the service refuses to start, and the provisioning script refuses to write
  such a file), nor `admin` or `human_verify`. The gate stamps `generated`, `verified` and
  `status`: a maintainer's changeset never verifies anything, whatever it writes.
- **No self-audit.** An auditor may review only the backlog the server derives and never a
  revision its own principal (or role) generated. Run the maintainer and the auditor under
  different OS users or hosts: under one uid either could read the other's token from
  `/proc/<pid>/environ`.
- **Evidence is never an agent's own text.** A process cites frozen work-item files; only a
  `human:` principal uploads a packet.
- **Quotas.** Changesets per hour and per day and deprecations per day are metered per
  principal (429 with `Retry-After`); the `maintainer` preset carries 30, 150 and 10.
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
- One principal per role serves any number of model swaps (§4). A second agent of a role that
  must run in parallel (auditors only; maintainers serialize on the lease anyway) gets its own
  id with the role in its name, for example `pp add auditor --id process:ai-wiki-auditor-mini`,
  so receipts tell the two apart.
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

Then preflight on the agent's own host, with its own token in the environment:

```bash
ai-wiki -b solvely-wiki doctor --role curator      # or --role auditor, --role member; exit 0
```

`doctor` fails closed (exit `4`) when the token's scopes are not exactly the role's, the
writer's API is newer than the CLI, the bundle is not served, or a tool (`git`, `uv`,
`multica`) is missing. Fix the host; never widen a token to pass it.

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
   stable string per run (1 to 128 of `A-Z a-z 0-9 _ . : @ / -`). A maintainer still needs a
   logged-in `multica` CLI on its host, whatever runs it: the issues collector reads the
   conversations through it, `doctor --role curator` checks for it, and a run whose collector
   is `unavailable` reports `status=blocked`. An auditor or a member needs no `multica`.

## 5. Run

- Maintainer: `max_concurrent_tasks=1`, at most 2 attempts, a 3 h task timeout. Its comment opens
  with the verbatim `maint end` report (skill §5). Progress lives on the writer (items,
  cursors, receipts), so a run can move to another host or model between any two runs.
- Auditor: its own schedule, never the maintainer's credential, never the maintainer's
  hand-off text: it reads only the backlog and the evidence the server serves.
- Member: `ai-wiki ingest <file|text>`; `ai-wiki jobs <id>` follows the work item until a
  changeset curates it (with the commit) or the maintainer skips it (with the reason).

## 6. Swap the model or the runtime

Nothing else changes: the principal, the token, the skills and the prompts stay. Only one
variable at a time, and canary it on the shadow bundle first:

```bash
multica agent update <agent-id> --model <model>              # a model of the same runtime
multica agent update <agent-id> --runtime-id <runtime-id>    # another runtime or host
```

- The canary: restore the shadow maintainer (`multica agent restore c10a1e06-8255-42ce-9b52-266aef42f2a6`),
  set the candidate model or runtime on it, give it one issue with the shadow prompt and
  `max_items=3`, and compare the receipts (gate first-pass rate, parks, `admin compare`).
  Archive it again after.
- A new host must pass `doctor` for the role (§3) before its first scheduled run.
- An auditor should stay on another model family than the maintainer's: independence comes
  from the model as well as from the principal.
- A maintainer's new runtime needs the `multica` CLI too (§4).

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
