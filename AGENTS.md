# Using ai-wiki (agent guide)

`ai-wiki` is a read-only window onto a curated **OKF knowledge bundle** served over an
HTTP API. The service runs no LLM — everything it returns is authored, verifiable content.
This guide is how an agent installs the CLI, points it at a knowledge base, and uses it.

## 1. Install the CLI

```bash
uv tool install git+https://github.com/Scorpion1221/ai-wiki      # provides the `ai-wiki` command
# or:  pipx install git+https://github.com/Scorpion1221/ai-wiki
# or:  git clone … && cd ai-wiki && uv run ai-wiki …
```

The client is stdlib-only; no extras needed. Config lives at `~/.ai-wiki/config.json`.

## 2. Connect, then pick a bundle

One server (a URL + token) hosts **many bundles** (knowledge bases). Connect once, then
list / switch / create the bundles that live on that server. (Ask the server's owner for
the endpoint + token.)

```bash
ai-wiki config set --endpoint https://<host>/ --token <token>   # connect to the server
ai-wiki bundle list             # bundles hosted there (* = active, default marked)
ai-wiki bundle use solvely-web  # switch the active bundle (saved locally)
ai-wiki bundle create my-kb     # create a new empty bundle on the server
ai-wiki bundle rm my-kb         # delete a bundle on the server (confirms; -y to skip)
ai-wiki -b other search "..."   # one-off: read a different bundle for this command
```

If the server hosts a single bundle (or sets a default), you can skip `bundle use` —
commands target the default automatically.

## 3. Read it — like a filesystem

```bash
ai-wiki health                       # size + counts by type/status
ai-wiki cat SCHEMA.md                # ORIENT FIRST: taxonomy + conventions
ai-wiki cat purpose.md               #              why this KB exists
ai-wiki ls                           # top-level dirs (like shell ls)
ai-wiki ls <dir>                     # drill into a directory
ai-wiki ls -R                        # recurse, flat (all concepts);  -a includes dotfiles
ai-wiki cat <dir>/<name>.md          # read a concept
ai-wiki search "<query>"             # ranked, CJK-aware
ai-wiki grep "<pattern>" [--fixed]   # regex (or literal with --fixed)
ai-wiki log                          # change ledger (what changed/was corrected)
```

**Navigation:** orient via `SCHEMA.md`/`purpose.md`, then either drill
(`ls` → `ls <dir>` → `cat`, following each concept's `# Related concepts` links to trace
relationships) or `search`/`grep` to jump to a term.

**Discipline:**
- Cite the concept paths you used (e.g. `metrics/<name>.md`).
- Trust only what the CLI returns — do not invent metrics, prices, event names, or dates.
- Respect `status` (draft/reviewed/canonical/stale) and `confidence`; many concepts are
  `draft`. Watch for `contested: true` or a `⚠️ …correction` note — report the corrected value.

## 4. Contribute knowledge (if the deployment allows writes)

You never edit concepts directly. You submit a *source*; a curation agent folds it into
the bundle as probationary concepts and flags contradictions.

```bash
ai-wiki ingest notes.md        # or:  cat notes.md | ai-wiki ingest -
ai-wiki jobs <job-id>          # poll curation status
```

Read-only deployments return `403` on `ingest` — that's expected.
