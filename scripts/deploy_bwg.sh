#!/usr/bin/env bash
# Deploy one exact ai-wiki commit to the AI Wiki host (bwg-9router since 2026-10-07):
# the writer (systemd ai-wiki-worker, /home/admin/app) and the read mirror (docker "ai-wiki").
#
# Refuses: within 10 minutes before a scheduled maintainer run (04:00 and 10:00-22:00 every 3 h CST since 2026-11-06;
# the auditor follows each one); while a writer job or a maint/audit lease is live;
# on a host that is not the AI Wiki writer (/etc/ai-wiki-moved present, or no writer unit);
# and, after the deploy, if the writer's /whoami modes changed. A failed writer or mirror health
# check rolls that part back.
#
# Usage: deploy_bwg.sh [--dry-run] <repo-dir> <commit>
#   --dry-run  run every check (window, host, idle, credentials, venv lock) and change nothing
# Env: AIWIKI_HOST_ALIAS (default bwg-9router).
set -euo pipefail

DRY=0; [ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
[ $# -eq 2 ] || { echo "usage: $0 [--dry-run] <repo-dir> <commit>" >&2; exit 2; }
HOST="${AIWIKI_HOST_ALIAS:-bwg-9router}"
REPO="$1"; SHA="$(git -C "$REPO" rev-parse --verify "$2^{commit}")"; SHORT="${SHA:0:7}"
VERSION="$(git -C "$REPO" show "$SHA:src/aiwiki/version.py" | sed -n 's/^VERSION = "\(.*\)"/\1/p')"
[ -n "$VERSION" ] || { echo "ABORT: no VERSION in $SHA:src/aiwiki/version.py" >&2; exit 2; }
TS="$(date -u +%Y%m%dT%H%M%SZ)"
hm=$(TZ=Asia/Shanghai date +%H%M)
# A run that starts mid-deploy would take its lease against a restarting writer: keep clear of each start.
now_min=$((10#${hm:0:2} * 60 + 10#${hm:2:2}))
for start in 0400 1000 1300 1600 1900 2200; do
  lead=$((10#${start:0:2} * 60 + 10#${start:2:2} - now_min))
  if [ "$lead" -ge 0 ] && [ "$lead" -le 10 ]; then
    echo "ABORT: the maintainer starts at ${start:0:2}:${start:2:2} CST; deploy after it ends" >&2; exit 2
  fi
done

TMPD="$(mktemp -d)"; TAR="$TMPD/ai-wiki-$SHORT.tar"
trap 'rm -rf "$TMPD"' EXIT
git -C "$REPO" archive --format=tar -o "$TAR" "$SHA"
[ "$DRY" = 1 ] || scp -q "$TAR" "$HOST:/var/tmp/ai-wiki-$SHORT.tar"
ssh -o BatchMode=yes "$HOST" DRY="$DRY" SHA="$SHA" SHORT="$SHORT" VERSION="$VERSION" TS="$TS" bash -s <<'REMOTE'
set -euo pipefail
APP=/home/admin/app
PROD=/home/admin/solvely-wiki
SHADOW=/var/lib/ai-wiki/bundles/solvely-wiki-shadow
SECRETS=/etc/ai-wiki-secrets
BUILD_ROOT=/var/lib/ai-wiki-build
STAGE="/var/tmp/ai-wiki-stage-$SHORT"
TARBALL="/var/tmp/ai-wiki-$SHORT.tar"

hdr=""
trap 'rm -f "$hdr"; rm -rf "$STAGE" "$TARBALL"' EXIT

# One deploy at a time on this host.
exec 9>/run/ai-wiki-deploy.lock
flock -n 9 || { echo "ABORT: another deploy holds /run/ai-wiki-deploy.lock" >&2; exit 3; }

# The right host: the live writer, not a retired one.
[ ! -e /etc/ai-wiki-moved ] || { echo "ABORT: $(hostname) is a retired AI Wiki host (/etc/ai-wiki-moved)" >&2; exit 3; }
systemctl cat ai-wiki-worker >/dev/null 2>&1 || { echo "ABORT: no ai-wiki-worker unit on $(hostname)" >&2; exit 3; }
systemctl is-active -q ai-wiki-worker || { echo "ABORT: ai-wiki-worker is not active; deploy onto a running writer" >&2; exit 3; }
for f in writer.env mirror.env; do
  [ -s "$SECRETS/$f" ] || { echo "ABORT: missing $SECRETS/$f" >&2; exit 3; }
done
echo "host $(hostname): writer $(cut -c1-7 "$APP/.ai-wiki-deployed-revision"), mirror $(docker inspect ai-wiki --format '{{.Config.Image}}')"

# The two read clones are pulled by oneshot units (ai-wiki-pull as root, ai-wiki-shadow-pull as admin). git's auto gc
# detaches by default, and systemd kills the detached gc when the unit's main process exits (KillMode=control-group):
# on 2026-10-28 that left a gc.pid.lock in /root/ai-wiki-bundles that blocked every later auto gc for 17 days
# (26.5 GiB of loose objects, disk 85%), while `git pull -q` kept exiting 0. Keep auto gc in the foreground of the pull,
# every deploy, and say so when a lock outlives an hour (no gc lasts that long here).
for clone in /root/ai-wiki-bundles /var/lib/ai-wiki/mirror/solvely-wiki-shadow; do
  [ -d "$clone/.git" ] || { echo "WARNING: read clone $clone is missing" >&2; continue; }
  owner=$(stat -c %U "$clone/.git")
  for key in gc.autoDetach maintenance.autoDetach; do
    if [ "$(sudo -u "$owner" git -C "$clone" config --get "$key" || true)" != false ]; then
      if [ "$DRY" = 1 ]; then echo "would set $key=false in $clone"
      else sudo -u "$owner" git -C "$clone" config "$key" false && echo "set $key=false in $clone"; fi
    fi
  done
  if [ -e "$clone/.git/gc.pid.lock" ] && [ $(( $(date +%s) - $(stat -c %Y "$clone/.git/gc.pid.lock") )) -gt 3600 ]; then
    echo "WARNING: $clone/.git/gc.pid.lock is older than 1 h: auto gc is blocked there (see the task 20261114-ai-wiki-mirror-git-gc)" >&2
  fi
done

# Idle: no queued or running writer job and no live maintainer or auditor lease, in either bundle.
active=$(sudo -u admin "$APP/.venv/bin/python" - "$PROD" "$SHADOW" <<'PY'
import glob, json, sys
from datetime import datetime, timezone
now, busy = datetime.now(timezone.utc), []
for bundle in sys.argv[1:]:
    for path in glob.glob(f"{bundle}/.okf/jobs/*.json"):
        try:
            if json.load(open(path)).get("status") in ("queued", "running"):
                busy.append(path)
        except Exception:
            pass
    for path in glob.glob(f"{bundle}/.okf/maint/lease-*.json"):
        try:
            expires = str(json.load(open(path)).get("expires_at") or "")
            if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) > now:
                busy.append(path)
        except Exception:
            busy.append(path + " (unreadable: inspect by hand)")
print("\n".join(busy))
PY
)
if [ -n "$active" ]; then
  if [ "$DRY" = 1 ]; then echo "WOULD ABORT: the writer is busy:"; echo "$active"
  else { echo "ABORT: the writer is busy:"; echo "$active"; } >&2; exit 3; fi
fi

# The writer's service token, from its root-only env file: kept off argv and out of the output.
hdr=$(mktemp); chmod 600 "$hdr"
python3 - "$SECRETS/writer.env" > "$hdr" <<'PY'
import sys
for line in open(sys.argv[1]):
    if line.startswith("AIWIKI_TOKEN="):
        print("Authorization: Bearer " + line.split("=", 1)[1].strip().strip('"').strip("'"))
        break
else:
    sys.exit("no AIWIKI_TOKEN in " + sys.argv[1])
PY
auth=(-H "@$hdr")
modes() { curl -fsS "${auth[@]}" http://127.0.0.1:8788/whoami 2>/dev/null \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("modes") or {}, sort_keys=True))' 2>/dev/null || echo '{}'; }
build_of() { curl -fsS "${auth[@]}" "$1/health" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("build") or "")' 2>/dev/null || true; }
MODES_BEFORE=$(modes)
[ "$MODES_BEFORE" != "{}" ] || { echo "ABORT: cannot read the writer's /whoami with its service token" >&2; exit 3; }
echo "writer modes: $MODES_BEFORE"

if [ "$DRY" = 1 ]; then
  sudo -u admin -H bash -c "cd $APP && UV_PYTHON=3.14.3 /usr/local/bin/uv sync --frozen --extra service --no-dev --dry-run 2>&1 | tail -1"
  disable=$(sed -n 's/^AIWIKI_DISABLE=//p' "$SECRETS/mirror.env")
  for need in ingest create delete changesets workspace maint admin audit; do
    case ",$disable," in *",$need,"*) ;; *) echo "WOULD ABORT: $SECRETS/mirror.env AIWIKI_DISABLE lacks '$need'" ;; esac
  done
  echo "mirror: $(docker inspect ai-wiki --format '{{len .Mounts}} mounts, {{.HostConfig.Memory}} bytes memory limit')"
  echo "embed cache: /var/lib/ai-wiki/embed-cache -> /embed-cache (rw); AIWIKI_EMBED_* in mirror.env: $(grep -c '^AIWIKI_EMBED_' "$SECRETS/mirror.env")"
  if grep -q '^AIWIKI_EMBED_' "$SECRETS/mirror.env" && ! grep -qx 'AIWIKI_EMBED_CACHE=/embed-cache' "$SECRETS/mirror.env"; then
    echo "WOULD ABORT: $SECRETS/mirror.env sets AIWIKI_EMBED_* without AIWIKI_EMBED_CACHE=/embed-cache"
  fi
  if [ -n "$active" ]; then echo "DRY RUN: every other check passed, but the writer is busy now"; exit 3; fi
  echo "DRY RUN OK: would deploy $SHA (writer + mirror ai-wiki:$VERSION-$SHORT); nothing changed"
  exit 0
fi

rm -rf "$STAGE"; mkdir -p "$STAGE"
tar -xf "$TARBALL" -C "$STAGE"

# --- writer: source tree, then the venv only when the lock or project metadata changed ---
backup="/home/admin/app.bak-$TS"
rsync -a --exclude .venv "$APP/" "$backup/"
deps_changed=0
cmp -s "$STAGE/uv.lock" "$APP/uv.lock" && cmp -s "$STAGE/pyproject.toml" "$APP/pyproject.toml" || deps_changed=1
rsync -a --delete --exclude .venv --exclude .ai-wiki-deployed-revision "$STAGE/" "$APP/"
echo "$SHA" > "$APP/.ai-wiki-deployed-revision"
chown -R admin:admin "$APP" "$backup"
if [ "$deps_changed" = 1 ]; then
  echo "uv.lock or pyproject.toml changed: syncing the venv"
  sudo -u admin -H bash -c "cd $APP && UV_PYTHON=3.14.3 /usr/local/bin/uv sync --frozen --extra service --no-dev >/dev/null" \
    || { echo "ABORT: uv sync failed; restoring the writer from $backup" >&2
         rsync -a --delete --exclude .venv "$backup/" "$APP/"; chown -R admin:admin "$APP"
         sudo -u admin -H bash -c "cd $APP && UV_PYTHON=3.14.3 /usr/local/bin/uv sync --frozen --extra service --no-dev >/dev/null" || true
         exit 4; }
fi

rollback_writer() {
  echo "rolling the writer back to $backup" >&2
  rsync -a --delete --exclude .venv "$backup/" "$APP/"; chown -R admin:admin "$APP"
  if [ "$deps_changed" = 1 ]; then
    sudo -u admin -H bash -c "cd $APP && UV_PYTHON=3.14.3 /usr/local/bin/uv sync --frozen --extra service --no-dev >/dev/null" || true
  fi
  systemctl restart ai-wiki-worker
  for i in $(seq 1 30); do [ -n "$(build_of http://127.0.0.1:8788)" ] && break; sleep 2; done
  echo "writer after rollback: build $(build_of http://127.0.0.1:8788 | cut -c1-7)" >&2
}
systemctl restart ai-wiki-worker
for i in $(seq 1 60); do [ "$(build_of http://127.0.0.1:8788)" = "$SHA" ] && break; sleep 2; done
if [ "$(build_of http://127.0.0.1:8788)" != "$SHA" ]; then
  echo "ABORT: the writer did not come up on $SHORT" >&2
  journalctl -u ai-wiki-worker -n 20 --no-pager >&2 || true
  rollback_writer; exit 4
fi
MODES_AFTER=$(modes)
if ! python3 -c 'import json,sys; a,b=json.loads(sys.argv[1]),json.loads(sys.argv[2]); sys.exit(0 if all(a[k]==b[k] for k in set(a)&set(b)) else 1)' "$MODES_BEFORE" "$MODES_AFTER"; then
  echo "ABORT: the deploy changed the writer modes: $MODES_BEFORE -> $MODES_AFTER" >&2
  rollback_writer; exit 4
fi
echo "writer: build $SHORT, modes unchanged"

# The daily watchdog runs a copy of scripts/maintenance_watchdog.py: install it when it changed.
if ! cmp -s "$APP/scripts/maintenance_watchdog.py" /usr/local/bin/ai-wiki-watchdog; then
  mkdir -p /var/backups && cp -a /usr/local/bin/ai-wiki-watchdog "/var/backups/ai-wiki-watchdog.$TS"
  install -m 0755 -o root -g root "$APP/scripts/maintenance_watchdog.py" /usr/local/bin/ai-wiki-watchdog
  echo "watchdog: installed the new script (previous in /var/backups/ai-wiki-watchdog.$TS)"
fi

# --- mirror: rebuild the image, recreate the container with the same mounts, env file and memory ---
mkdir -p "$BUILD_ROOT"; chmod 700 "$BUILD_ROOT"
rsync -a --delete "$STAGE/" "$BUILD_ROOT/"
old_image=$(docker inspect ai-wiki --format '{{.Config.Image}}')
mem=$(docker inspect ai-wiki --format '{{.HostConfig.Memory}}'); [ "$mem" != 0 ] || mem=""
mounts=(); while read -r m; do [ -n "$m" ] && mounts+=("$m"); done \
  < <(docker inspect ai-wiki --format '{{range .Mounts}}{{if ne .Destination "/embed-cache"}}--volume={{.Source}}:{{.Destination}}:ro{{println}}{{end}}{{end}}')
[ "${#mounts[@]}" -ge 1 ] || { echo "ABORT: cannot read the mirror's mounts" >&2; exit 5; }
# The embedding cache (2026-11-04, hybrid search) is the mirror's only writable mount: chunk vectors
# survive a redeploy instead of being embedded again (about 15 minutes of lexical-only answers).
EMBED_CACHE=/var/lib/ai-wiki/embed-cache
mkdir -p "$EMBED_CACHE"; chmod 700 "$EMBED_CACHE"
mounts+=("--volume=$EMBED_CACHE:/embed-cache:rw")
# The read mirror must never serve write or gate routes, whatever its env file says.
disable=$(sed -n 's/^AIWIKI_DISABLE=//p' "$SECRETS/mirror.env")
for need in ingest create delete changesets workspace maint admin audit; do
  case ",$disable," in *",$need,"*) ;; *) echo "ABORT: $SECRETS/mirror.env AIWIKI_DISABLE lacks '$need'" >&2; exit 5 ;; esac
done
# Hybrid search keeps its vectors in the rw mount; anywhere else they are lost on every redeploy.
if grep -q '^AIWIKI_EMBED_' "$SECRETS/mirror.env" && ! grep -qx 'AIWIKI_EMBED_CACHE=/embed-cache' "$SECRETS/mirror.env"; then
  echo "ABORT: $SECRETS/mirror.env sets AIWIKI_EMBED_* without AIWIKI_EMBED_CACHE=/embed-cache" >&2; exit 5
fi
docker build -q --build-arg "AIWIKI_BUILD_COMMIT=$SHA" -t "ai-wiki:$VERSION-$SHORT" "$BUILD_ROOT" >/dev/null
docker stop ai-wiki >/dev/null && docker rename ai-wiki "ai-wiki-prev-$TS"
restore_mirror() {
  docker rm -f ai-wiki >/dev/null 2>&1 || true
  docker rename "ai-wiki-prev-$TS" ai-wiki && docker start ai-wiki >/dev/null
}
if ! docker run -d --name ai-wiki --restart unless-stopped -p 127.0.0.1:8787:8787 \
     ${mem:+--memory "$mem"} "${mounts[@]}" --env-file "$SECRETS/mirror.env" \
     "ai-wiki:$VERSION-$SHORT" >/dev/null; then
  restore_mirror; echo "ABORT: mirror run failed; restored $old_image" >&2; exit 5
fi
for i in $(seq 1 60); do [ "$(build_of http://127.0.0.1:8787)" = "$SHA" ] && break; sleep 2; done
if [ "$(build_of http://127.0.0.1:8787)" != "$SHA" ]; then
  docker logs --tail 20 ai-wiki >&2 || true
  restore_mirror; echo "ABORT: mirror unhealthy; restored $old_image" >&2; exit 6
fi
docker rm "ai-wiki-prev-$TS" >/dev/null
echo "mirror: build $SHORT ($(docker inspect ai-wiki --format '{{.HostConfig.Memory}}') bytes memory limit)"

# Keep the two newest ai-wiki images (rollback), and the five newest writer backups.
docker images --format '{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}' | grep -P '\tai-wiki:' | sort -r | tail -n +3 | cut -f2 \
  | while read -r img; do [ "$img" = "ai-wiki:$VERSION-$SHORT" ] || docker rmi "$img" >/dev/null 2>&1 || true; done || true
ls -dt /home/admin/app.bak-* 2>/dev/null | tail -n +6 | xargs -r rm -rf || true
docker builder prune -f --filter until=168h >/dev/null 2>&1 || true

echo "DEPLOYED $SHA (writer backup $backup, previous mirror image $old_image)"
REMOTE
