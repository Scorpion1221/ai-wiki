# ai-wiki

A small **service + CLI** for serving and maintaining an [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog)
(Open Knowledge Format) markdown knowledge bundle.

Agents read the bundle like a filesystem — `ls` / `cat` / `grep` plus ranked,
CJK-aware search — over a token-authed HTTP API, so no one needs a full local clone.
They *maintain* it by **submitting a source**: a headless-agent curation pass folds the
source into the bundle as probationary concepts, flags contradictions, and runs the
deterministic close-out. Reads stay deterministic (no LLM in the service); only curation
uses an agent.

## Design

- **Engine** (`src/aiwiki/engine/`) — deterministic OKF maintenance: validate, source-drift
  detection, index generation, link/health lint, and update invariants (array-union,
  identity-lock, body-shrink guard). PyYAML + stdlib only; no LLM, no network.
- **Service** (`src/aiwiki/service/`) — FastAPI read API (`health/ls/cat/grep/search/log`)
  + write path (`POST /ingest`, `GET /jobs/{id}`). One server hosts **many bundles** under
  a single URL: list them with `GET /bundles`, pick one per request with `?bundle=<name>`,
  create/delete with `POST`/`DELETE /bundles`. Bearer-token auth, path sandboxing, and an
  `AIWIKI_DISABLE` switch for read-only / drill-only deployments.
- **CLI** (`src/aiwiki/cli/`) — `ai-wiki`, a thin stdlib-only client.
- **Runtime** (`src/aiwiki/runtime/`) — triggers a headless `claude -p` curation pass on
  ingest. The only LLM-using part; disable it (`AIWIKI_CURATE=off`) for a pure read deploy.

### Read/write split (multi-writer)

A public **read-only mirror** (`AIWIKI_DISABLE=ingest,create,delete`) and a team **ingest
worker** (curation enabled, with `claude` + a writable git remote) can be two deployments
of the same service. `POST /ingest` only *queues* a source; a single serial worker drains
the queue one job at a time (so concurrent submissions never race on the bundle/git),
rebases onto the remote before curating, commits, and pushes. On a rejected push it rebases
onto the moved remote and resolves any conflict with a second OKF-aware `claude` pass, then
retries (a push that still fails keeps the local commit). The mirror pulls the result.

## Quick start

```bash
uv sync --extra service --extra dev
uv run ai-wiki config set --endpoint http://127.0.0.1:8787 --token "$(python3 -c 'import secrets;print(secrets.token_hex(16))')"
AIWIKI_BUNDLES=./bundles ./run-local.sh      # serve a dir of bundles on :8787 (token from CLI config)

ai-wiki bundle list        # bundles hosted on the server (* = active)
ai-wiki bundle use <name>  # switch the active bundle (or `bundle create <name>`)
ai-wiki health
ai-wiki ls                 # list a level, like shell ls
ai-wiki cat <path>
ai-wiki search "<query>"
ai-wiki ingest notes.md    # submit a source for curation (needs `claude` + AIWIKI_CURATE!=off)
```

Engine CLIs are exposed as `okf-validate`, `okf-scan-sources`, `okf-lint`, etc.

## Configuration (env)

| Var | Meaning |
|-----|---------|
| `AIWIKI_BUNDLES` | dir holding one bundle per subdirectory (multi-bundle mode) |
| `AIWIKI_BUNDLE` | a single bundle dir (single-bundle mode; back-compat) |
| `AIWIKI_DEFAULT_BUNDLE` | bundle used when a request omits `?bundle=` (optional) |
| `AIWIKI_TOKEN` | bearer token clients must present |
| `AIWIKI_PORT` | service port (default 8787) |
| `AIWIKI_DISABLE` | comma-list of endpoints to 403 (e.g. `ingest,create,delete,search,grep`) |
| `AIWIKI_CURATE` | `auto` (default) or `off` to disable the curation trigger |

Requires Python ≥ 3.11. Licensed under Apache-2.0 (see LICENSE / NOTICE).
