# Codex integration: subscriptions, API wrappers, and read-only access

There are two independent directions:

1. **Reader:** Codex → `ai-wiki` CLI → service read endpoints. Reads invoke no server-side LLM.
2. **Writer:** authorized ingest/audit → service worker → configured Codex executable →
   guarded content pass → deterministic validation and Git closeout.

The optional `agent` object in the worker host's AI Wiki config selects the second direction.
It does not change how an already-running interactive Codex reader authenticates. See the
[configuration examples](../README.md#optional-codex-subscription--api-wrapper-selection).

## Subscription login

Leave `agent` absent, or select `bin: "codex"` and the desired model/effort. The service user
must already have a working Codex login. Do not transfer another machine's `auth.json` or
entire Codex home. Normal `codex` invocations keep their existing configuration.

The worker uses `--ignore-user-config` to avoid unrelated user plugins/provider settings;
Codex 0.154.0's CLI help explicitly states that this skips `config.toml` but still uses
`CODEX_HOME` for authentication. Consequently, an API wrapper must supply provider settings
as **command-line overrides**, not merely depend on that ignored config file.

This flag does not make Codex's own state read-only: 0.154.0 can still persist project-trust
entries for temporary workspaces. For API smoke tests, use a temporary `CODEX_HOME` (no
subscription credentials needed). For subscription tests, reuse the existing login and
clean up only the exact test-created trust entries; preserve unrelated concurrent edits.

## API-wrapper contract

The wrapper must:

- Accept and forward all worker arguments unchanged with `"$@"`.
- Use `exec` so timeout/process-group cleanup still controls Codex and its descendants.
- Load credentials from an external protected file or its inherited environment, without
  printing secrets or passing their values as command-line arguments.
- Fail on missing credentials; never silently switch to another account/provider.
- Not override the worker's sandbox, approval, tool-disable, or isolated-directory flags.

AI Wiki places its global `--config`/`--disable` options **before** `exec`. This matters
with Codex 0.154.0: adding overrides after `exec` can replace root-level overrides supplied
by a wrapper, dropping its custom provider and selecting `openai` instead. The command
builder and regression tests preserve the wrapper's provider arguments.

For the supplied Linux setup, an example `/root/.local/bin/codex-9router` is:

```bash
#!/usr/bin/env bash
set -euo pipefail
set +x
source /root/.config/secrets/codex-gateway.env
: "${CODEX_GATEWAY_API_KEY:?CODEX_GATEWAY_API_KEY is not set}"
export CODEX_GATEWAY_API_KEY
exec /usr/bin/codex \
  -c 'model="gpt-6-astra-combos"' \
  -c 'model_reasoning_effort="xhigh"' \
  -c 'model_provider="nine_router"' \
  -c 'model_providers.nine_router.name="9Router"' \
  -c 'model_providers.nine_router.base_url="https://9router.yqbqnn.com/v1"' \
  -c 'model_providers.nine_router.wire_api="responses"' \
  -c 'model_providers.nine_router.env_key="CODEX_GATEWAY_API_KEY"' \
  -c 'model_providers.nine_router.requires_openai_auth=false' \
  -c 'model_context_window=272000' \
  -c 'model_auto_compact_token_limit=258000' \
  "$@"
```

Keep the secret file mode `600` and wrapper mode `700`, owned by the service user. The secret
file contains the externally provisioned `CODEX_GATEWAY_API_KEY`; no key belongs in this
document, Git, command arguments, or test logs. These model/context values come from the
supplied deployment configuration, not an assertion about model availability or capacity.
Custom providers and environment-variable credentials are described in the official
[Codex configuration](https://learn.chatgpt.com/docs/config-file/config-advanced) and
[authentication](https://learn.chatgpt.com/docs/auth) documentation.

## Read-only access through an SSH tunnel

If the Wiki service is bound to the server's loopback address, a local CLI can use a tunnel:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18787:127.0.0.1:8787 aliyun-jp
```

Use a separate, mode-`600` client JSON config containing the endpoint
`http://127.0.0.1:18787`, the securely obtained **Wiki token** (not the model provider key),
and `bundle: "solvely-wiki"`. Select it with `AIWIKI_CONFIG=/absolute/path/to/client.json`
so the ordinary local CLI connection is not overwritten. Then run only:

```bash
ai-wiki health --json
ai-wiki ls --json
ai-wiki cat SCHEMA.md
ai-wiki cat purpose.md
```

Next, do one targeted `search`/`grep`, read the actual concepts, and follow a relevant
`links` hop when needed. Cite concept paths; apply status, trust, freshness, and
`verification_current` gates. `stable` alone does not prove correctness or freshness.

The tunnel is a connectivity mechanism, **not** a read-only authorization boundary. If
Codex itself runs the CLI, its tool sandbox must allow access to the tunnel endpoint. The
reported Linux 0.154.0 test used `workspace-write` with network access; that permits local
writes, even if the prompt forbids them. Treat this as behavioral restriction, not hard
read-only isolation. For a dedicated read-only service deployment, existing controls are:

```text
AIWIKI_DISABLE=ingest,audit,create,delete,changesets,workspace,maint,admin
AIWIKI_CURATE=off
```

Changing these on an existing writer affects other users; use a separately authorized
read-only deployment instead of silently disabling production maintenance. OS permissions
must separately prevent direct writes to bundle files and access to writer credentials.

The **writer** does not need tool-network access to read Wiki: it already receives local
bundle/source files. Do not copy the reader's `network_access=true` setting into the worker.

## Acceptance boundaries

- Verify config selection, model/effort, executable permissions, and ordinary-Codex isolation.
- Test both executable forms with isolated fake subprocesses before any live model request.
- A minimal wrapper inference proves the provider path and accepted worker flags, not Wiki
  curation quality, successful audit, or Git delivery.
- Tunnel checks use GET/read commands only. Stop the tunnel and delete temporary client
  credentials after testing; never run `ingest`/`audit` merely to check connectivity.
- End-to-end maintenance requires explicit authorization for a target bundle, input sources,
  audit, Git commit/push, and rollback. Ingest completion is not independent verification.
- Local tests do not mean the server has received the change. Deploy/restart and live
  `writer_agent` readback are separate steps requiring authorization.

## Validation record — 2026-09-20 (Asia/Shanghai)

- Local targeted regression: **222 passed** across config/CLI, curation/audit, service reads,
  worker/Git recovery, and maintenance. Ruff and `git diff --check` passed. The suite emitted
  one existing FastAPI/Starlette test-client deprecation warning.
- `ssh aliyun-jp`: Linux, Codex **0.154.0**; wrapper mode **700**, secret mode **600**.
- Local CLI through a temporary loopback-only SSH tunnel: `health`, `ls`, `cat SCHEMA.md`,
  and `cat purpose.md` succeeded for `solvely-wiki`. Service **0.2.6**, OKF **0.2**, 268
  concepts; the health snapshot reported 196 machine-confirmed / 72 unverified and
  1 fresh / 7 stale / 260 unspecified. These are a dated snapshot, not fresh fact certification.
- The new config loader and command builder were exercised on Linux in temporary directories,
  without installing code or changing the running service. API wrapper inference passed:
  provider **nine_router**, model **gpt-6-astra-combos**, effort **xhigh**, exact output
  `AIWIKI_AGENT_OK`, exit **0**, with the worker sandbox flags retained.
- Subscription selection used the existing ChatGPT login, provider **openai**, model
  **gpt-5.6-sol**, effort **high**. Inference was blocked by the account's usage limit;
  a successful subscription model response remains unverified. No reset or credit purchase
  was attempted, and no automatic fallback to the API wrapper occurred.
- Tests exposed and fixed root/subcommand provider-override loss. Temporary trust entries
  created by Codex were removed with scoped cleanup; protected config, auth, wrapper, and
  secret files matched their pre-retest fingerprints afterward.
- No Wiki ingest/audit, content edits, service restart, or deployment was performed. SSH
  tunnels were stopped and temporary credential/config/workspace files removed.
