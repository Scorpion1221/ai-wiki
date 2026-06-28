#!/usr/bin/env bash
# Launch the ai-wiki service locally.
#
# Reads the bearer token from the CLI config (~/.ai-wiki/config.json) so the
# server and the `ai-wiki` client always share one token — no token to copy around.
#
#   ./run-local.sh                            # serve the bundles in ~/Downloads/ai-wiki-bundles
#   AIWIKI_BUNDLES=/path/to/bundles ./run-local.sh   # a dir holding one bundle per subdir
#   AIWIKI_BUNDLE=/path/to/one-bundle ./run-local.sh # single-bundle (back-compat)
#   AIWIKI_PORT=9000 ./run-local.sh           # different port
#   AIWIKI_DISABLE=search,grep ./run-local.sh # drill-only mode
set -euo pipefail

CONFIG="${AIWIKI_CONFIG:-$HOME/.ai-wiki/config.json}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$CONFIG" ]; then
  echo "no CLI config at $CONFIG — connect first:" >&2
  echo "  uv run --project $HERE ai-wiki config set --endpoint http://127.0.0.1:${AIWIKI_PORT:-8787} --token \$(python3 -c 'import secrets;print(secrets.token_hex(16))')" >&2
  exit 1
fi

# Token: prefer an explicit AIWIKI_TOKEN, else the configured connection token. Supports
# the current {endpoint,token,bundle} schema and the older {current,bundles:{...}} one.
TOKEN="${AIWIKI_TOKEN:-$(python3 -c "import json;c=json.load(open('$CONFIG'));print(c.get('token') or (c['bundles'][c['current']]['token'] if 'bundles' in c else ''))")}"
export AIWIKI_TOKEN="$TOKEN" AIWIKI_PORT="${AIWIKI_PORT:-8787}"

# Multi-bundle (a dir of bundles) unless a single bundle was named.
if [ -n "${AIWIKI_BUNDLE:-}" ]; then
  export AIWIKI_BUNDLE
  echo "serving single bundle $AIWIKI_BUNDLE on http://127.0.0.1:$AIWIKI_PORT  (disabled: ${AIWIKI_DISABLE:-none})"
else
  export AIWIKI_BUNDLES="${AIWIKI_BUNDLES:-$HOME/Downloads/ai-wiki-bundles}"
  echo "serving bundles in $AIWIKI_BUNDLES on http://127.0.0.1:$AIWIKI_PORT  (disabled: ${AIWIKI_DISABLE:-none})"
fi
exec uv run --project "$HERE" python -m aiwiki.service
