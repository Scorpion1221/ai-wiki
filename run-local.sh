#!/usr/bin/env bash
# Launch the ai-wiki service locally.
#
# Reads the bearer token from the CLI config (~/.config/ai-wiki/config.json) so the
# server and the `ai-wiki` client always share one token — no token to copy around.
#
#   ./run-local.sh                       # serve ./bundle on :8787
#   AIWIKI_PORT=9000 ./run-local.sh      # different port
#   AIWIKI_BUNDLE=/path/to/bundle ./run-local.sh
#   AIWIKI_DISABLE=search,grep ./run-local.sh   # drill-only mode
set -euo pipefail

BUNDLE="${AIWIKI_BUNDLE:-$PWD/bundle}"
CONFIG="${AIWIKI_CONFIG:-$HOME/.config/ai-wiki/config.json}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$CONFIG" ]; then
  echo "no CLI config at $CONFIG — run first:" >&2
  echo "  uv run --project $HERE ai-wiki config set --endpoint http://127.0.0.1:${AIWIKI_PORT:-8787} --token \$(python3 -c 'import secrets;print(secrets.token_hex(16))')" >&2
  exit 1
fi

TOKEN="$(python3 -c "import json;print(json.load(open('$CONFIG'))['token'])")"
export AIWIKI_BUNDLE="$BUNDLE" AIWIKI_TOKEN="$TOKEN" AIWIKI_PORT="${AIWIKI_PORT:-8787}"
echo "serving $BUNDLE on http://127.0.0.1:$AIWIKI_PORT  (disabled: ${AIWIKI_DISABLE:-none})"
exec uv run --project "$HERE" python -m aiwiki.service
