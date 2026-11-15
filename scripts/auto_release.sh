#!/usr/bin/env bash
# Take one low-risk ai-wiki release to production without the owner's word, or stop and say why.
#
# The owner (2026-11-16) lets an agent merge and deploy by itself a release that cannot change what the writer
# stores, what the gate or the audit accepts, or how production is operated; scripts/release_gate.py decides
# which a diff is. This script is the only path that merges such a release: every condition is checked here,
# from the repository, GitHub and the host, never taken from the caller's word.
#
#   auto_release.sh check   <repo-dir> <pr>     every condition, nothing changed
#   auto_release.sh release <repo-dir> <pr>     check, merge pinned to the head, deploy, verify; roll back on failure
#   auto_release.sh verify  <sha>               the post-deploy checks alone (production serves <sha>, healthy)
#
# Exit codes (the caller's next step):
#   0  check: every condition holds. release: merged, deployed and verified. verify: healthy on <sha>.
#   2  usage, or a tool or host could not be reached. Nothing changed.
#   3  the release needs the owner (the gate says ask, or a condition does not hold). Nothing changed.
#   4  release: merged, but the writer stayed busy past the wait. Not deployed; run `release` again later.
#   5  release: deployed and failed verification; production was rolled back to the previous commit and is
#      healthy. main still holds the merge: open a revert PR and tell the owner.
#   6  release: the rollback failed too. Production needs a person now.
#
# Conditions for `check` and `release`:
#   - the PR is open, targets main, is mergeable, and main has no commit the PR's head lacks;
#   - CI is green on the PR's head (both jobs completed with success);
#   - release_gate.py classify <merge-base> <head> says auto;
#   - AUTO_RELEASE_REVIEW (a command, run with the head sha appended) exits 0: the caller's review record for
#     exactly this head. Without it `release` refuses: a release nobody reviewed never goes by itself.
#     scripts/review_covers.py <reviews-dir> <repo-dir> <repo-id> is such a command: the latest round approved,
#     nothing blocking open, and the commit it reviewed has the head's tree;
#   - production serves main's code (the deployed commit has main's tree: nothing else is half-deployed);
#   - fewer than AUTO_RELEASE_PER_DAY (default 1) releases went out by this script in the last 24 h.
#
# Env: AIWIKI_HOST_ALIAS (default bwg-9router), AIWIKI_URL (default the public read URL in the CLI's config),
#      AUTO_RELEASE_WAIT_S (default 5400: how long `release` waits for an idle writer),
#      AUTO_RELEASE_LOG (default ~/.ai-wiki/auto-release.log, one line per release).
set -euo pipefail

usage() { sed -n '9,11p' "$0" | sed 's/^# \{0,3\}//' >&2; exit 2; }
say() { echo "auto_release: $*"; }
stop() { echo "auto_release: STOP ($1) $2" >&2; exit "$1"; }

VERB="${1:-}"; shift || true
HOST="${AIWIKI_HOST_ALIAS:-bwg-9router}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="${AUTO_RELEASE_LOG:-$HOME/.ai-wiki/auto-release.log}"
PER_DAY="${AUTO_RELEASE_PER_DAY:-1}"
WAIT_S="${AUTO_RELEASE_WAIT_S:-5400}"

# What production serves and whether it is healthy: one line `writer=<sha> mirror=<sha> errors=<n> worker=<state>`.
production() {
  ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" SINCE="${1:-}" bash -s <<'REMOTE'
set -euo pipefail
writer=$(cat /home/admin/app/.ai-wiki-deployed-revision 2>/dev/null || echo unknown)
mirror=$(docker inspect ai-wiki --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^AIWIKI_BUILD_COMMIT=//p')
errors=0
if [ -n "$SINCE" ]; then
  errors=$(( $(journalctl -u ai-wiki-worker --since "$SINCE" --no-pager 2>/dev/null | grep -ciE 'traceback|\berror\b|exception' || true) \
           + $(docker logs ai-wiki --since "$SINCE" 2>&1 | grep -ciE 'traceback|\berror\b|exception' || true) ))
fi
echo "writer=$writer mirror=${mirror:-unknown} errors=$errors worker=$(systemctl is-active ai-wiki-worker || true) restarts=$(docker inspect ai-wiki --format '{{.RestartCount}}' 2>/dev/null || echo -1)"
REMOTE
}
field() { local kv; for kv in $2; do if [ "${kv%%=*}" = "$1" ]; then echo "${kv#*=}"; return; fi; done; }

verify() {  # <sha> [<since, UTC>]: production serves <sha> and answers; prints why not
  local sha="$1" since="${2:-}" state health
  state=$(production "$since") || { echo "the host did not answer"; return 1; }
  [ "$(field writer "$state")" = "$sha" ] || { echo "the writer is on $(field writer "$state" | cut -c1-7), not ${sha:0:7}"; return 1; }
  [ "$(field mirror "$state")" = "$sha" ] || { echo "the mirror is on $(field mirror "$state" | cut -c1-7), not ${sha:0:7}"; return 1; }
  [ "$(field worker "$state")" = "active" ] || { echo "ai-wiki-worker is $(field worker "$state")"; return 1; }
  [ "$(field restarts "$state")" = "0" ] || { echo "the mirror container restarted $(field restarts "$state") time(s)"; return 1; }
  [ "$(field errors "$state")" = "0" ] || { echo "$(field errors "$state") error line(s) in the writer or mirror log since $since"; return 1; }
  health=$(ai-wiki health --json 2>/dev/null) || { echo "ai-wiki health failed from this host"; return 1; }
  python3 -I -c '
import json, sys
d = json.loads(sys.argv[1])
bad = [k for k, want in (("build", sys.argv[2]), ("compatible", True)) if d.get(k) != want]
bad += ["concepts"] if not d.get("concepts") else []
sys.exit("health: " + ", ".join(bad) if bad else 0)' "$health" "$sha" || return 1
  ai-wiki search "ai-wiki" --top-k 3 2>/dev/null | grep -q 'mode: "hybrid"' || { echo "a search did not answer in hybrid mode"; return 1; }
}

if [ "$VERB" = "verify" ]; then
  [ $# -eq 1 ] || usage
  why=$(verify "$1") && { say "production is healthy on ${1:0:7}"; exit 0; }
  stop 3 "$why"
fi
{ [ "$VERB" = "check" ] || [ "$VERB" = "release" ]; } && [ $# -eq 2 ] || usage
REPO="$1"; PR="$2"
for tool in git gh ssh python3 ai-wiki; do command -v "$tool" >/dev/null || stop 2 "$tool is not on PATH"; done
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || stop 2 "$REPO is not a git repository"
git -C "$REPO" fetch -q origin || stop 2 "git fetch failed in $REPO"

meta=$(cd "$REPO" && gh pr view "$PR" --json state,baseRefName,headRefOid,mergeable,isDraft 2>/dev/null) || stop 2 "cannot read PR $PR"
read -r STATE BASE HEAD MERGEABLE DRAFT < <(python3 -I -c '
import json, sys
d = json.loads(sys.argv[1]); print(d["state"], d["baseRefName"], d["headRefOid"], d["mergeable"], d["isDraft"])' "$meta")
MAIN=$(git -C "$REPO" rev-parse origin/main)
[ "$STATE" = "OPEN" ] && [ "$DRAFT" = "False" ] || stop 3 "PR $PR is $STATE (draft: $DRAFT)"
[ "$BASE" = "main" ] || stop 3 "PR $PR targets $BASE, not main"
[ "$MERGEABLE" = "MERGEABLE" ] || stop 3 "PR $PR is $MERGEABLE"
git -C "$REPO" cat-file -e "$HEAD^{commit}" 2>/dev/null || stop 2 "the PR's head ${HEAD:0:7} is not in $REPO after a fetch"
git -C "$REPO" merge-base --is-ancestor "$MAIN" "$HEAD" || stop 3 "main (${MAIN:0:7}) has commits the PR's head lacks: bring the release up to date"

# Check names hold spaces ("test (3.11)"): read them as JSON, never split on whitespace.
checks=$(cd "$REPO" && gh api "repos/{owner}/{repo}/commits/$HEAD/check-runs" --jq '[.check_runs[] | {name, status, conclusion}]') \
  || stop 2 "cannot read CI for ${HEAD:0:7}"
ci=$(python3 -I -c '
import json, sys
try:
    runs = json.loads(sys.argv[1] or "[]")
except ValueError:
    sys.exit("unreadable")
bad = ["%s %s/%s" % (r.get("name"), r.get("status"), r.get("conclusion")) for r in runs
       if (r.get("status"), r.get("conclusion")) != ("completed", "success")]
if bad or len(runs) < 2:
    sys.exit(", ".join(bad) or "%d check(s) ran, 2 expected" % len(runs))' "$checks" 2>&1) \
  || stop 3 "CI is not green on ${HEAD:0:7}: $ci"

set +e; gate=$(python3 -I "$HERE/release_gate.py" classify "$REPO" "$MAIN" "$HEAD"); code=$?; set -e
[ "$code" = 0 ] || { echo "$gate" >&2; [ "$code" = 3 ] && stop 3 "the change is not low-risk: it waits for the owner"; stop 2 "release_gate.py failed"; }
say "gate: $(head -1 <<<"$gate")"

if [ -n "${AUTO_RELEASE_REVIEW:-}" ]; then
  bash -c "$AUTO_RELEASE_REVIEW $HEAD" >/dev/null 2>&1 || stop 3 "the review record does not approve ${HEAD:0:7} (AUTO_RELEASE_REVIEW exited non-zero)"
  say "review: approved for ${HEAD:0:7}"
elif [ "$VERB" = "release" ]; then
  stop 3 "AUTO_RELEASE_REVIEW is not set: a release nobody reviewed does not go by itself"
else
  say "review: not checked (set AUTO_RELEASE_REVIEW; release refuses without it)"
fi

# Production serves the commit that was deployed (a release head); main holds the merge commit of that release.
# The two differ as commits and agree as trees, so the code is what is compared.
tree_of() { git -C "$REPO" rev-parse --verify --quiet "$1^{tree}" 2>/dev/null || true; }
state=$(production) || stop 2 "the host $HOST did not answer"
PREV=$(field writer "$state")
main_tree=$(tree_of "$MAIN")
[ -n "$main_tree" ] && [ "$(tree_of "$PREV")" = "$main_tree" ] && [ "$(tree_of "$(field mirror "$state")")" = "$main_tree" ] \
  || stop 3 "production (writer ${PREV:0:7}, mirror $(field mirror "$state" | cut -c1-7)) does not serve main's code (${MAIN:0:7}): something else is being released"
recent=0
[ -f "$LOG" ] && recent=$(python3 -I -c '
import sys, datetime
now = datetime.datetime.now(datetime.timezone.utc); n = 0
for line in open(sys.argv[1]):
    words = line.split()
    if not words or words[-1] != "merged":  # one release writes several lines; its merge is the one counted
        continue
    try: n += (now - datetime.datetime.fromisoformat(words[0].replace("Z", "+00:00"))).total_seconds() < 86400
    except Exception: n += 1  # a line that cannot be dated counts: the limit fails closed
print(n)' "$LOG")
[ "$recent" -lt "$PER_DAY" ] || stop 3 "$recent release(s) went out by this script in the last 24 h (limit $PER_DAY)"
say "conditions hold: PR $PR ${HEAD:0:7} -> main ${MAIN:0:7}, production on ${PREV:0:7}"
[ "$VERB" = "check" ] && exit 0

# --- release: merge pinned to the head, then deploy the head (main's tree is the head's tree) ---
(cd "$REPO" && gh pr merge "$PR" --merge --match-head-commit "$HEAD" >/dev/null) || stop 3 "the merge was refused; nothing changed"
git -C "$REPO" fetch -q origin main
[ "$(git -C "$REPO" rev-parse 'origin/main^{tree}')" = "$(git -C "$REPO" rev-parse "$HEAD^{tree}")" ] \
  || stop 4 "merged, but main's tree is not the release's tree: not deployed"
mkdir -p "$(dirname "$LOG")"
echo "$(date -u +%FT%TZ) pr=$PR head=$HEAD previous=$PREV merged" >> "$LOG"
say "merged PR $PR; waiting for an idle writer (up to ${WAIT_S}s)"

deploy() {  # <sha>: main's deploy script, which refuses a busy writer and rolls a failed part back itself
  git -C "$REPO" show "origin/main:scripts/deploy_bwg.sh" > "$TMP/deploy_bwg.sh"
  bash "$TMP/deploy_bwg.sh" "$REPO" "$1" 2>&1 | sed -E 's/(TOKEN|SECRET|KEY)=[^ ]+/\1=<redacted>/g'
  return "${PIPESTATUS[0]}"
}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
deadline=$(( $(date +%s) + WAIT_S )); since=""
while :; do
  since=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
  set +e; out=$(deploy "$HEAD"); code=$?; set -e
  [ "$code" = 0 ] && break
  if grep -qE 'ABORT: (the writer is busy|the maintainer starts|another deploy holds)' <<<"$out" && [ "$(date +%s)" -lt "$deadline" ]; then
    sleep 60; continue
  fi
  if grep -qE 'ABORT: (the writer is busy|the maintainer starts|another deploy holds)' <<<"$out"; then
    stop 4 "merged, but the writer stayed busy for ${WAIT_S}s: not deployed; run release again later"
  fi
  echo "$out" | tail -8 >&2
  # deploy_bwg.sh restores the part it broke; confirm production is back on the previous commit.
  why=$(verify "$PREV") && stop 5 "the deploy failed (exit $code) and production is healthy on the previous ${PREV:0:7}; main holds the merge: open a revert PR"
  stop 6 "the deploy failed (exit $code) and production is not healthy on ${PREV:0:7}: $why"
done
grep -E '^DEPLOYED ' <<<"$out" || true

if why=$(verify "$HEAD" "$since"); then
  echo "$(date -u +%FT%TZ) pr=$PR head=$HEAD previous=$PREV deployed" >> "$LOG"
  say "released: production is healthy on ${HEAD:0:7} (previous ${PREV:0:7})"
  exit 0
fi
say "verification failed: $why; rolling production back to ${PREV:0:7}"
set +e; back=$(deploy "$PREV"); code=$?; set -e
if [ "$code" = 0 ] && again=$(verify "$PREV"); then
  echo "$(date -u +%FT%TZ) pr=$PR head=$HEAD previous=$PREV rolled_back reason=$(tr ' ' '_' <<<"$why")" >> "$LOG"
  stop 5 "verification failed ($why); production is rolled back to ${PREV:0:7} and healthy; main holds the merge: open a revert PR"
fi
echo "$back" | tail -8 >&2
stop 6 "verification failed ($why) and the rollback to ${PREV:0:7} did not verify (${again:-deploy exit $code})"
