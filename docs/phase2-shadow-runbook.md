# Phase 2 shadow runbook (aliyun-jp)

This runbook deploys the Phase 2 shadow of the design (§9 "影子对比"): a second bundle,
`solvely-wiki-shadow`, that the new changeset gate commits to while production keeps
running exactly as today. Every step has its own verification and rollback, and nothing
in it writes to production's bundle, its clone or its origin.

What changes, in order:

| § | Change | Production effect |
|---|---|---|
| 2 | Principals file `/etc/ai-wiki/principals.json` | none until §5 loads it |
| 3 | Bare origin `/var/lib/ai-wiki/shadow.git`, seeded from production HEAD (R0) | none |
| 4 | Writer bundles root `/var/lib/ai-wiki/bundles` with the shadow clone and a link to production | none until §5 |
| 5 | Writer drop-ins: principals, then multi-bundle mode with the shadow | same bundle path, same default, same legacy token |
| 6 | Read mirror: shadow read clone, principals, shadow mount | reads unchanged |
| 7 | Cloudflare ingress: writer routes for the gate | `/whoami` now answered by the writer; `/ingest`, `/jobs` as before |
| 8 | Watchdog: second `--bundle` | one more check |
| 9 | Shadow audit timer (12:00 CST, oldest 5) | Codex time, 5 audits a day at most |

§10 verifies the whole path, including a production dry-run over a sample of concepts
with the owner token and zero 5xx. §11 hands the shadow over to its Multica agent. §12
rolls back any step or all of them.

Requires: writer and mirror deployed from a build that contains the linked-bundle fix
(`service/bundle.py:discover`), `scripts/dry_run_sample.py`,
`scripts/provision_principals.py` and the mirror image's `safe.directory` for `/bundles/*`
(`Dockerfile`: the container runs Git as root, and the shadow's read clone is admin's); §1 P1
checks it.

## 0. Layout: why production is linked, not moved

The writer runs today in single-bundle mode (`AIWIKI_BUNDLE=/home/admin/solvely-wiki`).
One writer can only host a second bundle in multi-bundle mode (`AIWIKI_BUNDLES=<root>`),
where every bundle is an entry of the root. Production's bundle stays where it is, and the
root holds a symlink to it:

```text
/var/lib/ai-wiki/                       admin:admin 0750
├── shadow.git/                         bare origin of solvely-wiki-shadow (push semantics as production)
├── bundles/                            AIWIKI_BUNDLES of the writer
│   ├── solvely-wiki -> /home/admin/solvely-wiki     production, unchanged path
│   └── solvely-wiki-shadow/            the writer's clone of shadow.git
└── mirror/
    └── solvely-wiki-shadow/            clean read clone for the docker mirror
```

The service maps a root entry that is a symlink to a bundle directory **of the same name**
to that real directory (`service/bundle.py:discover`), so the writer serves
`/home/admin/solvely-wiki`, byte for byte the path single-bundle mode serves:

- the read gates, which refuse any path that goes through a symlink, see no symlink;
- the Git-root checks of curate, audit, revert and restart recovery (`bundle` must be its
  repository's root) see the same repository;
- job receipts stay in `/home/admin/solvely-wiki/.okf/jobs`, where the watchdog, the deploy
  guard and every existing receipt point;
- worker checks keyed on the bundle name (`AIWIKI_CHANGESETS_COMMIT`,
  `AIWIKI_CODEX_AUDIT_MANUAL`) see `solvely-wiki`, because a link must keep its target's name;
- a symlink inside the bundle is still refused;
- `DELETE /bundles/solvely-wiki` answers 409 wherever bundle deletion is enabled: the link's
  target is not the server's to remove (this runbook also disables it on the writer: 403).

`tests/test_shadow_layout.py` covers each of these with the real service code: reads,
dry-run and committed changesets, a symlinked concept, an ingest's job path, a Codex audit
commit, a revert and its crash recovery, all on a linked bundle. Without the fix, a symlink
in the root is ignored (the old `is_bundle` refuses symlinks), so this layout needs a build
that contains it.

Moving production into the root instead (`mv /home/admin/solvely-wiki
/var/lib/ai-wiki/bundles/`) would also work, but it changes the path the writer unit, the
watchdog and the deploy guard name, all at once and with the writer stopped, and its
rollback is another move. The link changes none of them, and removing it is the whole
rollback.

## 1. Pre-flight (read-only)

Run everything in §1–§6 and §8–§9 on aliyun-jp as root (`ssh aliyun-jp`, bash). Pick a
window outside 03:30–06:30 CST with no maintainer run in progress; the idle check below is
the gate for each restart.

Define the helpers in every new shell:

```bash
export PROD=/home/admin/solvely-wiki
export BASE=/var/lib/ai-wiki
export ORIGIN=$BASE/shadow.git
export ROOT=$BASE/bundles
export SHADOW=$ROOT/solvely-wiki-shadow
export READCLONE=$BASE/mirror/solvely-wiki-shadow
export P2=/root/ai-wiki-phase2             # backups, R0, header files (root 0700)
as_admin() { sudo -u admin "$@"; }
pp() { env PYTHONDONTWRITEBYTECODE=1 /home/admin/app/.venv/bin/python \
         /home/admin/app/scripts/provision_principals.py --file /etc/ai-wiki/principals.json "$@"; }
legacy_token() { systemctl show ai-wiki-worker -p Environment --value | tr ' ' '\n' | sed -n 's/^AIWIKI_TOKEN=//p'; }
active_jobs() {  # queued or running writer jobs in the given bundles
  as_admin /home/admin/app/.venv/bin/python - "$@" <<'PY'
import glob, json, sys
n = 0
for bundle in sys.argv[1:]:
    for path in glob.glob(f"{bundle}/.okf/jobs/*.json"):
        try:
            n += json.load(open(path)).get("status") in ("queued", "running")
        except Exception:
            pass
print(n)
PY
}
```

Checks (each must print what its comment says):

```bash
# P1. The deployed build contains the linked-bundle fix, provision_principals.py and dry_run_sample.py.
grep -q 'target.name == p.name' /home/admin/app/src/aiwiki/service/bundle.py && echo fix-present
ls /home/admin/app/scripts/provision_principals.py /home/admin/app/scripts/dry_run_sample.py
cat /home/admin/app/.ai-wiki-deployed-revision                     # BUILD
docker inspect ai-wiki --format '{{.Config.Image}}'                # ai-wiki:0.3.x-<BUILD short>
docker exec ai-wiki git config --system --get-all safe.directory   # /bundles/*

# P2. The writer is in the state this runbook starts from.
systemctl show ai-wiki-worker -p DropInPaths --value              # codex-runtime.conf okf-v02.conf zz-agent-config.conf
systemctl show ai-wiki-worker -p Environment --value | tr ' ' '\n' | grep '^AIWIKI_' | cut -d= -f1,2 | grep -v TOKEN
#   AIWIKI_BUNDLES=  AIWIKI_DEFAULT_BUNDLE=solvely-wiki  AIWIKI_BUNDLE=/home/admin/solvely-wiki  AIWIKI_HOST=127.0.0.1
#   AIWIKI_PORT=8788  AIWIKI_CURATE=auto  AIWIKI_CONFIG=/home/admin/.ai-wiki/config.json  AIWIKI_AGENT_* (unset at exec)
#   and no AIWIKI_PRINCIPALS, AIWIKI_DISABLE, AIWIKI_CHANGESETS_*, AIWIKI_CODEX_AUDIT_MANUAL
grep -c '^AIWIKI_' /etc/environment                                # 0
grep '^ExecStart=' /etc/systemd/system/ai-wiki-watchdog.service    # the line §8 extends, unchanged
test ! -e $BASE && echo no-var-lib-ai-wiki
ls -A /etc/ai-wiki | wc -l; stat -c '%U:%G %a' /etc/ai-wiki          # 0; root:root 700

# P3. Production's clone is clean, on main, and published (ls-remote only reads).
as_admin git -C $PROD status --porcelain | wc -l                  # 0
as_admin git -C $PROD for-each-ref --format='%(refname)' refs/heads # refs/heads/main only
as_admin git -C $PROD rev-parse HEAD origin/main                  # the same sha twice
as_admin git -C $PROD ls-remote origin refs/heads/main             # the same sha

# P4. Room on disk (the shadow's history packs to ~10 MB; three copies).
df -BG --output=avail /var/lib | tail -1                           # >= 2G

# P5. The mirror and the writer hold the same legacy token; the mirror has one mount.
[ "$(legacy_token | sha256sum)" = "$(docker inspect ai-wiki --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | sed -n 's/^AIWIKI_TOKEN=//p' | sha256sum)" ] && echo same-legacy-token
docker inspect ai-wiki --format '{{json .Mounts}}' | jq -c '[.[] | [.Source, .Destination, .RW]]'
#   [["/root/ai-wiki-bundles","/bundles/solvely-wiki",false]]

# P6. The live tunnel ingress (read from cloudflared itself).
curl -s http://127.0.0.1:20242/config | jq -c '{version, ingress: [.config.ingress[] | {hostname, path, service}]}'
#   version 2: ^/(ingest|jobs) -> http://localhost:8788, the rest of ai-wiki.yqbqnn.com -> http://localhost:8787, http_status:404

# P7. Idle.
active_jobs $PROD                                                  # 0
```

Back up what later steps change, and write the legacy token's header file (a header file
keeps tokens out of `ps`):

```bash
install -d -m 0700 $P2
cp -a /etc/systemd/system/ai-wiki-worker.service.d $P2/worker.service.d.before
cp -a /etc/systemd/system/ai-wiki-watchdog.service $P2/
docker inspect ai-wiki > $P2/mirror-inspect.before.json
( umask 077; printf 'Authorization: Bearer %s\n' "$(legacy_token)" > $P2/legacy.h )
curl -s -H @$P2/legacy.h http://127.0.0.1:8788/health | jq -c '{bundle, concepts, build}' | tee $P2/health.before.json
```

If any check fails, stop: this runbook assumes that exact starting state.

## 2. Principals

Provision the principals the shadow needs, through `scripts/provision_principals.py`. The
file only takes effect when §5 and §6 load it. Each `add` prints its token once on stdout,
straight into a root-only file.

```bash
chgrp admin /etc/ai-wiki && chmod 0750 /etc/ai-wiki      # the writer (admin) reads the file
AIWIKI_TOKEN="$(legacy_token)" pp add-legacy              # eb17 keeps working, as member:legacy-token
( umask 077; set -o noclobber                            # a rerun never empties a captured token
  pp add owner             > $P2/owner.token              # human:guobaoqi, aiw_h_, every scope
  pp add shadow-maintainer > $P2/shadow-maintainer.token  # process:ai-wiki-maintainer-shadow, aiw_c_, shadow only
  pp add auditor --id process:ai-wiki-shadow-audit --bundle solvely-wiki-shadow \
                           > $P2/shadow-audit.token       # read+audit on the shadow: the §9 timer
)
# eb17 keeps every scope, but only on the bundle it reaches today: members neither see nor
# ingest into the shadow (a shadow ingest would spend Codex time).
jq '(.principals[] | select(.id == "member:legacy-token")) += {"bundles": ["solvely-wiki"]}' \
   /etc/ai-wiki/principals.json > /etc/ai-wiki/.principals.json.new
chown root:admin /etc/ai-wiki/.principals.json.new && chmod 0640 /etc/ai-wiki/.principals.json.new
mv /etc/ai-wiki/.principals.json.new /etc/ai-wiki/principals.json
( umask 077; for name in owner shadow-maintainer shadow-audit; do
    printf 'Authorization: Bearer %s\n' "$(cat $P2/$name.token)" > $P2/$name.h
  done )
```

On a rerun, `noclobber` refuses each redirect whose token file exists, so that `add` never
runs. An empty token file means its `add` failed after the shell created the file: remove the
id if `pp list` shows it, delete the empty file, and rerun.

Verify:

```bash
AIWIKI_TOKEN="$(legacy_token)" pp check
#   ok: 4 principals: member:legacy-token, human:guobaoqi, process:ai-wiki-maintainer-shadow, process:ai-wiki-shadow-audit
#   legacy token: held by member:legacy-token
pp list | jq -c '.[] | {id, role, bundles}'
stat -c '%U:%G %a' /etc/ai-wiki /etc/ai-wiki/principals.json      # root:admin 750, root:admin 640
```

The owner now copies the owner token into the password manager
(`ssh aliyun-jp cat /root/ai-wiki-phase2/owner.token`); the laptop steps (§7, §10) read it
with `read -rs`. §11 removes every token file from the host.

Rollback: `rm /etc/ai-wiki/principals.json; chgrp root /etc/ai-wiki; chmod 0700 /etc/ai-wiki`
(only once §5a and §6 no longer load it).

## 3. Shadow bare origin

R0 is production's published HEAD now; the shadow is never reset to another baseline. §1's
P3 and P7 ran a while ago, so the seeding checks again that no production job can move HEAD
and that HEAD is what GitHub serves: a local commit a curation has not pushed yet may still
be rebased or reset, and a baseline production never publishes breaks `admin compare --since`.

```bash
install -d -o admin -g admin -m 0750 $BASE $ROOT $BASE/mirror
R0=$(as_admin git -C $PROD rev-parse HEAD)
if [ "$(active_jobs $PROD)" = 0 ] && [ -z "$(as_admin git -C $PROD status --porcelain)" ] \
   && [ "$R0" = "$(as_admin git -C $PROD ls-remote origin refs/heads/main | cut -f1)" ]; then
  echo "$R0" > $P2/R0
  as_admin git clone -q --bare --no-local $PROD $ORIGIN           # packed copy; shares no files with production
  as_admin git -C $ORIGIN update-ref refs/heads/main "$R0"        # R0, even if production committed meanwhile
  as_admin git -C $ORIGIN remote remove origin                    # never fetch from production again
  as_admin git -C $ORIGIN config receive.denyNonFastForwards true # like a protected main
  as_admin git -C $ORIGIN config receive.denyDeletes true
else
  echo 'STOP: production has a job or an unpublished HEAD; nothing was seeded, rerun when idle'
fi
```

Verify:

```bash
as_admin git -C $ORIGIN for-each-ref --format='%(refname) %(objectname)'   # refs/heads/main <R0>, nothing else
as_admin git -C $ORIGIN symbolic-ref HEAD                                  # refs/heads/main
as_admin git -C $ORIGIN fsck --no-dangling --connectivity-only && echo fsck-ok
as_admin git -C $ORIGIN config --get remote.origin.url || echo no-origin
du -sh $ORIGIN
```

Rollback: `rm -rf $ORIGIN` (after §4 and §6 are rolled back).

## 4. The writer's clone and bundles root

```bash
as_admin git clone -q $ORIGIN $SHADOW
as_admin mkdir -p $SHADOW/.okf/jobs          # ignored by Git; the watchdog (§8) errors on a bundle without it
as_admin ln -s $PROD $ROOT/solvely-wiki
```

Verify:

```bash
ls -l $ROOT                                                    # solvely-wiki -> /home/admin/solvely-wiki, solvely-wiki-shadow/
as_admin git -C $SHADOW rev-parse --show-toplevel HEAD         # /var/lib/ai-wiki/bundles/solvely-wiki-shadow, R0
as_admin git -C $SHADOW remote get-url origin                  # /var/lib/ai-wiki/shadow.git
as_admin git -C $SHADOW status --porcelain | wc -l             # 0
test -f $SHADOW/SCHEMA.md && echo bundle-ok
as_admin git -C $PROD status --porcelain | wc -l               # 0: production untouched
```

The shadow's commits carry the writer's identity from `/home/admin/.gitconfig`
(`ai-wiki-worker`), as production's do.

Rollback: `rm $ROOT/solvely-wiki && rm -rf $SHADOW`, only after §5b is rolled back (its
`/whoami` check): a writer still in multi-bundle mode finds no bundles without the root and
answers 503 on every production route. `rm` of the link removes the link only; never
`rm -rf $ROOT/solvely-wiki/`.

## 5. Writer drop-ins

Two restarts, one variable each. Before each: `active_jobs $PROD` (and `$SHADOW` once it is
served) prints 0.

### 5a. Principals

```bash
cat > /etc/systemd/system/ai-wiki-worker.service.d/phase1-principals.conf <<'EOF'
[Service]
Environment=AIWIKI_PRINCIPALS=/etc/ai-wiki/principals.json
EOF
systemctl daemon-reload
[ "$(active_jobs $PROD)" = 0 ] && systemctl restart ai-wiki-worker || echo 'writer busy: rerun when idle'
for i in $(seq 60); do curl -fsS -o /dev/null -H @$P2/legacy.h http://127.0.0.1:8788/health && break; sleep 2; done
```

Verify:

```bash
systemctl is-active ai-wiki-worker                                                   # active
curl -s -H @$P2/legacy.h http://127.0.0.1:8788/whoami | jq -c '{writer, principal, bundles}'
#   {"writer":true,"principal":"member:legacy-token","bundles":["solvely-wiki"]}
curl -s -H @$P2/owner.h  http://127.0.0.1:8788/whoami | jq -c '{principal, role, auth}'
#   human:guobaoqi, admin, auth.principals lists the four ids, auth.reload_error null
curl -s -H @$P2/legacy.h http://127.0.0.1:8788/health | jq -c '{bundle, concepts, build}'   # as health.before.json
journalctl -u ai-wiki-worker --since -10min --no-pager | grep -iE 'traceback|error' || echo clean
```

Rollback: `rm /etc/systemd/system/ai-wiki-worker.service.d/phase1-principals.conf &&
systemctl daemon-reload && systemctl restart ai-wiki-worker` (idle first). The legacy token
then authenticates alone again; the aiw_ tokens stop working.

### 5b. Multi-bundle mode with the shadow

```bash
cat > /etc/systemd/system/ai-wiki-worker.service.d/phase2-shadow.conf <<'EOF'
# Phase 2 shadow (docs/phase2-shadow-runbook.md). Sorts after okf-v02.conf, which it overrides.
[Service]
Environment=AIWIKI_BUNDLE=
Environment=AIWIKI_BUNDLES=/var/lib/ai-wiki/bundles
Environment=AIWIKI_DEFAULT_BUNDLE=solvely-wiki
Environment=AIWIKI_CHANGESETS_COMMIT=solvely-wiki-shadow
Environment=AIWIKI_CODEX_AUDIT_MANUAL=solvely-wiki-shadow
# Single-bundle mode refused bundle create/delete; keep refusing them.
Environment=AIWIKI_DISABLE=create,delete
EOF
systemctl daemon-reload
systemctl show ai-wiki-worker -p Environment --value | tr ' ' '\n' | grep -E '^AIWIKI_(BUNDLES?|DEFAULT_BUNDLE|CHANGESETS_COMMIT|CODEX_AUDIT_MANUAL|DISABLE|PRINCIPALS)='
[ "$(active_jobs $PROD)" = 0 ] && systemctl restart ai-wiki-worker || echo 'writer busy: rerun when idle'
for i in $(seq 60); do curl -fsS -o /dev/null -H @$P2/legacy.h http://127.0.0.1:8788/health && break; sleep 2; done
```

Startup recovery now also scans the shadow; a shadow that cannot be recovered would stop
the writer from starting, so the first check is that it is active.

Verify:

```bash
systemctl is-active ai-wiki-worker                                                   # active
curl -s -H @$P2/owner.h http://127.0.0.1:8788/whoami | jq -c .modes
#   {"intake":"curate","audit":"codex","changesets_commit":["solvely-wiki-shadow"],"restructure":"off",
#    "codex_audit_manual":["solvely-wiki-shadow"]}
curl -s -H @$P2/legacy.h http://127.0.0.1:8788/health | jq -c '{bundle, concepts, build}'   # production, as before
curl -s -H @$P2/owner.h 'http://127.0.0.1:8788/health?bundle=solvely-wiki-shadow' | jq -c '{bundle, concepts, okf_version}'
#   solvely-wiki-shadow, the same concept count at R0, "0.2"
curl -s -o /dev/null -w '%{http_code}\n' -H @$P2/legacy.h 'http://127.0.0.1:8788/health?bundle=solvely-wiki-shadow'   # 403
curl -s -D - -o /dev/null -H @$P2/owner.h 'http://127.0.0.1:8788/workspace?bundle=solvely-wiki-shadow' | grep -i x-aiwiki-revision   # R0
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE -H @$P2/owner.h http://127.0.0.1:8788/bundles/solvely-wiki   # 403 (disabled)
curl -s -H @$P2/legacy.h 'http://127.0.0.1:8788/jobs/pending-audit?older_than_hours=24' | jq -c '{shown, total}'   # production, as before
ls $PROD/.okf/jobs | wc -l; ls $SHADOW/.okf/jobs | wc -l                             # production's count unchanged; 0
journalctl -u ai-wiki-worker --since -10min --no-pager | grep -iE 'traceback|error' || echo clean
```

Rollback: `rm /etc/systemd/system/ai-wiki-worker.service.d/phase2-shadow.conf &&
systemctl daemon-reload && systemctl restart ai-wiki-worker` (with `active_jobs $PROD $SHADOW`
at 0). The writer is back on `AIWIKI_BUNDLE=/home/admin/solvely-wiki` once the legacy
`/whoami` shows `.modes.changesets_commit` `[]`; a restart skipped while busy still shows
`["solvely-wiki-shadow"]`. Production lost
nothing: its jobs were written to its own `.okf/jobs` all along. Shadow state stays on disk
until §4 is rolled back.

## 6. Read mirror: the shadow and the principals

The mirror serves a clean clone of the shadow's published branch, as it does for
production, never the writer's working tree, whose ignored `.okf/` holds receipts, leases
and frozen evidence.

```bash
as_admin git clone -q $ORIGIN $READCLONE
cat > /etc/systemd/system/ai-wiki-shadow-pull.service <<'EOF'
[Unit]
Description=ai-wiki shadow read clone git pull (read mirror)
[Service]
Type=oneshot
User=admin
Environment=HOME=/home/admin
WorkingDirectory=/var/lib/ai-wiki/mirror/solvely-wiki-shadow
ExecStart=/usr/bin/git pull -q --ff-only
EOF
cat > /etc/systemd/system/ai-wiki-shadow-pull.timer <<'EOF'
[Unit]
Description=pull the ai-wiki shadow read clone
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload && systemctl enable --now ai-wiki-shadow-pull.timer
systemctl start ai-wiki-shadow-pull.service && systemctl is-failed ai-wiki-shadow-pull.service   # "inactive"
```

Recreate the container with the same image and environment, plus `AIWIKI_PRINCIPALS`, the
principals directory (a directory mount sees the file's atomic replacement) and the shadow:

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ); echo "$TS" > $P2/mirror-TS
IMAGE=$(docker inspect ai-wiki --format '{{.Config.Image}}'); echo "$IMAGE" > $P2/mirror-image
ENVFILE=/root/ai-wiki/.mirror-env-$TS
docker inspect ai-wiki --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep '^AIWIKI_' | grep -v '^AIWIKI_BUILD_COMMIT=' > $ENVFILE
echo 'AIWIKI_PRINCIPALS=/etc/ai-wiki/principals.json' >> $ENVFILE
# With two bundles mounted, a read that names none (eb17's) needs the default.
grep -q '^AIWIKI_DEFAULT_BUNDLE=solvely-wiki$' $ENVFILE || echo 'AIWIKI_DEFAULT_BUNDLE=solvely-wiki' >> $ENVFILE
chmod 600 $ENVFILE; grep -o '^AIWIKI_[A-Z_]*' $ENVFILE
#   AIWIKI_BUNDLES AIWIKI_DEFAULT_BUNDLE AIWIKI_HOST AIWIKI_PORT AIWIKI_DISABLE AIWIKI_CURATE AIWIKI_TOKEN AIWIKI_PRINCIPALS
docker stop ai-wiki >/dev/null && docker rename ai-wiki ai-wiki-prev-$TS
docker run -d --name ai-wiki --restart unless-stopped -p 127.0.0.1:8787:8787 \
  -v /root/ai-wiki-bundles:/bundles/solvely-wiki:ro \
  -v /var/lib/ai-wiki/mirror/solvely-wiki-shadow:/bundles/solvely-wiki-shadow:ro \
  -v /etc/ai-wiki:/etc/ai-wiki:ro \
  --env-file $ENVFILE "$IMAGE"
for i in $(seq 60); do curl -fsS -o /dev/null -H @$P2/legacy.h http://127.0.0.1:8787/health && break; sleep 2; done
```

If the new container does not answer, restore the old one at once:

```bash
docker rm -f ai-wiki; docker rename ai-wiki-prev-$TS ai-wiki; docker start ai-wiki
```

Verify:

```bash
curl -s -H @$P2/legacy.h http://127.0.0.1:8787/health | jq -c '{bundle, concepts, build}'    # production, as before
curl -s -H @$P2/legacy.h http://127.0.0.1:8787/bundles | jq -c .                               # solvely-wiki only
curl -s -H @$P2/owner.h  http://127.0.0.1:8787/bundles | jq -c .                               # both, default solvely-wiki
curl -s -H @$P2/shadow-maintainer.h 'http://127.0.0.1:8787/health?bundle=solvely-wiki-shadow' \
  | jq -c '{bundle, okf_version, git_revision}'   # git_revision: $(as_admin git -C $ORIGIN rev-parse main), never null
curl -s -H @$P2/owner.h  http://127.0.0.1:8787/whoami | jq -c '{writer, principal}'           # writer false: the mirror
ls -A $READCLONE | grep -c '^.okf$'                                                            # 0: no writer state in the read clone
```

A null `git_revision` means the image lacks the `/bundles/*` `safe.directory` (P1): Git in the
container runs as root and refuses the admin-owned clone.

Keep `ai-wiki-prev-$TS` until §10 passes, then `docker rm ai-wiki-prev-$(cat $P2/mirror-TS)`.

**Deploys from now on.** The mirror's environment now names `AIWIKI_PRINCIPALS`, and the
deploy procedure carries the environment over from `docker inspect` but not the mounts. A
`docker run` with only the production mount therefore cannot read
`/etc/ai-wiki/principals.json`: the service refuses to import (`PrincipalsError`), and the
container crash-loops, which takes all public reads down. The writer also serves the shadow's
jobs now. Until §12 has run, every deploy must:

1. count the shadow's jobs in its writer idle guard, next to production's:

   ```python
   paths = glob.glob("/home/admin/solvely-wiki/.okf/jobs/*.json") \
       + glob.glob("/var/lib/ai-wiki/bundles/solvely-wiki-shadow/.okf/jobs/*.json")
   ```

2. recreate the mirror with the live container's mounts, read before the old container is
   stopped and renamed:

   ```bash
   mounts=(); while read -r m; do [ -n "$m" ] && mounts+=("$m"); done \
     < <(docker inspect ai-wiki --format '{{range .Mounts}}--volume={{.Source}}:{{.Destination}}:ro{{println}}{{end}}')
   docker run -d --name ai-wiki --restart unless-stopped -p 127.0.0.1:8787:8787 "${mounts[@]}" \
     --env-file "$ENVFILE" "$IMAGE"
   ```

   Afterwards `docker inspect ai-wiki --format '{{json .Mounts}}'` lists the three mounts.
   Both changes do nothing before §6 and after §12, so the deploy procedure can keep them.

Rollback: recreate the mirror from the build it runs now, with the production mount only and
no principals. This works whether or not `ai-wiki-prev-*` still exists (§10 removes it) and
whatever a deploy has changed since §6; the shadow's pull timer and read clone go only once
the single-mount mirror answers.

```bash
RTS=$(date -u +%Y%m%dT%H%M%SZ)
IMAGE=$(docker inspect ai-wiki --format '{{.Config.Image}}')
ENVFILE=/root/ai-wiki/.mirror-env-$RTS
docker inspect ai-wiki --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^AIWIKI_' \
  | grep -v -e '^AIWIKI_BUILD_COMMIT=' -e '^AIWIKI_PRINCIPALS=' > $ENVFILE; chmod 600 $ENVFILE
if [ -n "$IMAGE" ] && grep -q '^AIWIKI_TOKEN=.' $ENVFILE; then
  docker stop ai-wiki >/dev/null && docker rename ai-wiki ai-wiki-shadow-$RTS
  docker run -d --name ai-wiki --restart unless-stopped -p 127.0.0.1:8787:8787 \
    -v /root/ai-wiki-bundles:/bundles/solvely-wiki:ro --env-file $ENVFILE "$IMAGE"
  for i in $(seq 60); do curl -fsS -o /dev/null -H @$P2/legacy.h http://127.0.0.1:8787/health && break; sleep 2; done
  if curl -fsS -o /dev/null -H @$P2/legacy.h http://127.0.0.1:8787/health; then
    docker rm ai-wiki-shadow-$RTS >/dev/null; docker rm ai-wiki-prev-$(cat $P2/mirror-TS) 2>/dev/null
    systemctl disable --now ai-wiki-shadow-pull.timer
    rm -f /etc/systemd/system/ai-wiki-shadow-pull.{service,timer} && systemctl daemon-reload
    rm -rf $READCLONE
    echo mirror-rolled-back
  else
    docker rm -f ai-wiki; docker rename ai-wiki-shadow-$RTS ai-wiki; docker start ai-wiki
    echo 'STOP: the single-mount mirror did not answer; the three-mount mirror is back'
  fi
else
  echo 'STOP: cannot read the live mirror image or its legacy token; nothing was changed'
fi
```

Verify: `mirror-rolled-back` printed, `docker inspect ai-wiki --format '{{json .Mounts}}'`
shows the production mount only, and the legacy `/health` on 8787 answers `solvely-wiki` with
production's concept count.

## 7. Cloudflare ingress

Run this on the owner's laptop with a Cloudflare API token that may edit tunnels
(Account > Cloudflare Tunnel > Edit). The PUT replaces the whole configuration, so the body
is derived from the current one and checked against the reviewed body before it is sent.

```bash
read -rs CF_API_TOKEN && export CF_API_TOKEN
TUNNEL=fadfca50-cda7-4241-a56c-b9ea91eb2969          # ai-wiki.yqbqnn.com (cloudflared /diag/tunnel)
CF_ACCOUNT_ID=$(curl -fsS -H "Authorization: Bearer $CF_API_TOKEN" https://api.cloudflare.com/client/v4/accounts \
  | jq -r '.result[] | select(.id | startswith("3880d6e7")) | .id')
API=https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TUNNEL/configurations
mkdir -p ~/ai-wiki-phase2 && cd ~/ai-wiki-phase2
curl -fsS -H "Authorization: Bearer $CF_API_TOKEN" "$API" | jq .result > tunnel-before.json
jq -c '{version, config}' tunnel-before.json
jq '{config: (.config | .ingress |= map(if .service == "http://localhost:8788"
      then .path = "^/(ingest|jobs|whoami|workspace|changesets|maint|audit/backlog|admin)" else . end))}' \
   tunnel-before.json > tunnel-put.json
cat > tunnel-expected.json <<'EOF'
{"config": {"ingress": [
  {"hostname": "ai-wiki.yqbqnn.com", "path": "^/(ingest|jobs|whoami|workspace|changesets|maint|audit/backlog|admin)",
   "service": "http://localhost:8788"},
  {"hostname": "ai-wiki.yqbqnn.com", "service": "http://localhost:8787"},
  {"service": "http_status:404"}],
  "warp-routing": {"enabled": false}}}
EOF
# Sent only when there is exactly one writer rule on today's path and the derived body is the
# reviewed one (an empty originRequest is the API's rule without origin settings; it is sent back as is).
if jq -e '.config.ingress | map(select(.service == "http://localhost:8788")) | length == 1 and .[0].path == "^/(ingest|jobs)"' \
      tunnel-before.json >/dev/null \
   && diff <(jq -S 'del(.config.ingress[].originRequest | select(. == {}))' tunnel-put.json) <(jq -S . tunnel-expected.json); then
  curl -fsS -X PUT -H "Authorization: Bearer $CF_API_TOKEN" -H 'Content-Type: application/json' \
       --data @tunnel-put.json "$API" | jq -c '{success, version: .result.version}'
else
  echo 'STOP: the live ingress is not the reviewed one; nothing was sent'
fi
```

`tunnel-expected.json` is the body derived from the configuration read on 2026-09-25
(version 2). Only the writer rule's `path` changes; both rules keep `hostname`, and the mirror
still answers everything else, `/health` included. On `STOP`, the configuration changed since,
or the API spells it differently (for example `"warp-routing": {}` for `{"enabled": false}`):
compare `tunnel-put.json` with `tunnel-before.json`, and only once the sole change is the
writer rule's `path`, send that body with the same `curl -X PUT` line.

Verify on aliyun-jp (cloudflared picks up the new version within seconds) and from the laptop:

```bash
curl -s http://127.0.0.1:20242/config | jq -c '{version, path: .config.ingress[0].path}'   # version 3, the new path
```

```bash
read -rs OWNER && export OWNER                         # the aiw_h_ owner token, from the password manager
H="Authorization: Bearer $OWNER"
curl -s -H "$H" https://ai-wiki.yqbqnn.com/whoami | jq -c '{writer, principal}'             # writer true
curl -s -H "$H" https://ai-wiki.yqbqnn.com/health | jq -c '{bundle, writer_agent}'          # writer_agent null: the mirror
curl -s -D - -o /dev/null -H "$H" 'https://ai-wiki.yqbqnn.com/workspace?bundle=solvely-wiki-shadow' | grep -i x-aiwiki-revision
curl -s -H "$H" 'https://ai-wiki.yqbqnn.com/maint/status?bundle=solvely-wiki-shadow' | jq -c 'keys'   # the writer answers
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" 'https://ai-wiki.yqbqnn.com/jobs/pending-audit'      # 200, as before
```

Rollback (laptop):

```bash
jq '{config: .config}' ~/ai-wiki-phase2/tunnel-before.json > ~/ai-wiki-phase2/tunnel-rollback.json
curl -fsS -X PUT -H "Authorization: Bearer $CF_API_TOKEN" -H 'Content-Type: application/json' \
     --data @$HOME/ai-wiki-phase2/tunnel-rollback.json "$API" | jq -c '{success, version: .result.version}'
```

## 8. Watchdog: the shadow's writer checks

Do this on the day the shadow agent's first run is scheduled: the shadow's newest commit
is R0 until then, and the commit-age check alerts after 48h.

```bash
mkdir -p /etc/systemd/system/ai-wiki-watchdog.service.d
cat > /etc/systemd/system/ai-wiki-watchdog.service.d/phase2-shadow.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/home/admin/app/.venv/bin/python /usr/local/bin/ai-wiki-watchdog --bundle /home/admin/solvely-wiki --bundle /var/lib/ai-wiki/bundles/solvely-wiki-shadow --state-file /var/lib/ai-wiki-watchdog/writer.json --label aliyun-jp-writer
EOF
systemctl daemon-reload
systemctl show ai-wiki-watchdog -p ExecStart --value | grep -o -- '--bundle [^ ]*'
```

Verify with a replay at the current instant, which never notifies:

```bash
as_admin /home/admin/app/.venv/bin/python /usr/local/bin/ai-wiki-watchdog \
  --bundle /home/admin/solvely-wiki --bundle /var/lib/ai-wiki/bundles/solvely-wiki-shadow \
  --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | jq -c '{status, checks: (.checks | keys), alerts: [.alerts[].key]}'
#   checks include writer:solvely-wiki and writer:solvely-wiki-shadow
```

Rollback: `rm /etc/systemd/system/ai-wiki-watchdog.service.d/phase2-shadow.conf &&
systemctl daemon-reload`.

## 9. Shadow audit timer

The shadow's Codex audits are not queued by its changesets (`AIWIKI_CODEX_AUDIT_MANUAL`).
At 12:00 CST this requests the audit of the five oldest unaudited shadow changesets, with a
token that may only read and audit the shadow. A failed audit leaves its changeset pending
and among the oldest, so a changeset whose audit already failed three times (`AUDIT_ATTEMPTS`
of `ai-wiki maint`) is skipped rather than retried every day ahead of newer ones: it needs a
human. The script lists those in its journal, and the watchdog's
`writer:solvely-wiki-shadow` check (§8) alerts on each failed audit that no later attempt
resolved, for a week.

```bash
( umask 077; printf 'AIWIKI_TOKEN=%s\n' "$(cat $P2/shadow-audit.token)" > /etc/ai-wiki-shadow-audit.env )
install -d -o admin -g admin -m 0750 $BASE/shadow-audit
echo '{"endpoint": "http://127.0.0.1:8788", "bundle": "solvely-wiki-shadow"}' > $BASE/shadow-audit/config.json
chown admin:admin $BASE/shadow-audit/config.json
cat > /usr/local/sbin/ai-wiki-shadow-audit <<'EOF'
#!/bin/bash
# Phase 2 (design §9): Codex audits of the 5 oldest unaudited solvely-wiki-shadow changesets.
set -uo pipefail
pending=$(ai-wiki -b solvely-wiki-shadow jobs --pending-audit --older-than-hours 0 --json) || exit 1
stuck=$(jq -r '.jobs[] | select((.failed_audit_attempts | length) >= 3) | .id' <<<"$pending")
[ -z "$stuck" ] || echo "needs a human, audit failed 3 times:" $stuck
ids=$(jq -r '[.jobs[] | select((.failed_audit_attempts | length) < 3)][:5][].id' <<<"$pending")
failed=0
for id in $ids; do  # a refused request fails the unit but never holds back the others
  ai-wiki -b solvely-wiki-shadow audit "$id" --json | jq -c '{id, parent_job, status, deduplicated}' || failed=1
done
exit $failed
EOF
chmod 0755 /usr/local/sbin/ai-wiki-shadow-audit
cat > /etc/systemd/system/ai-wiki-shadow-audit.service <<'EOF'
[Unit]
Description=Codex audits of the oldest solvely-wiki-shadow changesets (phase 2)
After=ai-wiki-worker.service
[Service]
Type=oneshot
User=admin
Environment=HOME=/home/admin
Environment=AIWIKI_CONFIG=/var/lib/ai-wiki/shadow-audit/config.json
EnvironmentFile=/etc/ai-wiki-shadow-audit.env
ExecStart=/usr/local/sbin/ai-wiki-shadow-audit
EOF
cat > /etc/systemd/system/ai-wiki-shadow-audit.timer <<'EOF'
[Unit]
Description=Daily 12:00 CST Codex audits of the shadow (phase 2)
[Timer]
OnCalendar=*-*-* 12:00:00
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload && systemctl enable --now ai-wiki-shadow-audit.timer
```

Verify:

```bash
systemctl list-timers ai-wiki-shadow-audit.timer --no-pager      # next 12:00 CST
systemctl start ai-wiki-shadow-audit.service; systemctl is-failed ai-wiki-shadow-audit.service   # "inactive"
journalctl -u ai-wiki-shadow-audit -n 20 --no-pager              # nothing to audit yet, or one line per request
```

Rollback:

```bash
systemctl disable --now ai-wiki-shadow-audit.timer
rm /etc/systemd/system/ai-wiki-shadow-audit.{service,timer} /usr/local/sbin/ai-wiki-shadow-audit /etc/ai-wiki-shadow-audit.env
systemctl daemon-reload && rm -rf $BASE/shadow-audit
pp remove process:ai-wiki-shadow-audit
kill -HUP "$(pgrep -P "$(systemctl show -p MainPID --value ai-wiki-worker)" -f aiwiki.service)"   # the writer rereads the file
docker kill -s HUP ai-wiki                                                                           # the mirror too
```

## 10. Verification

Run on the laptop from an ai-wiki checkout at the deployed revision
(`git checkout $(ssh aliyun-jp cat /home/admin/app/.ai-wiki-deployed-revision)`), with the
owner token in the environment and a throwaway CLI config, so the saved config stays as it is:

```bash
export AIWIKI_CONFIG=$(mktemp -d)/config.json
echo '{"endpoint": "https://ai-wiki.yqbqnn.com/"}' > $AIWIKI_CONFIG
read -rs AIWIKI_TOKEN && export AIWIKI_TOKEN             # owner, aiw_h_
```

V1. The public `/whoami` is the writer's, it knows the owner, and the modes are as set
(the curator `doctor` runs on the runtime host in §11; the owner holds more than a curator's
scopes, so `doctor --role curator` fails for this token by design):

```bash
curl -s -H "Authorization: Bearer $AIWIKI_TOKEN" https://ai-wiki.yqbqnn.com/whoami \
  | jq -c '{writer, principal, api, modes}'
#   writer true, human:guobaoqi, {"changesets":1}, changesets_commit and codex_audit_manual ["solvely-wiki-shadow"]
```

V2. **Production dry-run over a sample of concepts, zero 5xx.** Each concept gets a
footnoted claim citing an uploaded probe packet, and each dry-run is sent exactly once
(`ai-wiki propose --dry-run` resends a 5xx, which would hide it). A dry-run creates no job,
takes no lock and touches no Git. Each reads the whole published tree, about four seconds,
so sixty take about five minutes. A rehearsal of this layout against a clone of production
at 1a6079e judged all 305 concepts `would_apply`, with no 5xx.

```bash
uv run python scripts/dry_run_sample.py -b solvely-wiki --count 60 > dry-run-prod.json; echo "exit $?"   # exit 0
jq -c '{bundle, base_revision, sent, skipped, http, server_errors: (.server_errors | length)}' dry-run-prod.json
#   server_errors 0; http has no 5xx key
jq -r '.rows[] | select(.status == "rejected") | "\(.path) \(.codes | join(","))"' dry-run-prod.json   # the gate's 422s, for review
```

And one literal `propose --dry-run` through the CLI, on a new probe concept:

```bash
ws=$(mktemp -d); uv run ai-wiki -b solvely-wiki workspace pull --dir $ws/ws
uv run ai-wiki concept new references/phase2-probe.md --dir $ws/ws --type Reference --title "Phase 2 probe" \
  --description "A dry-run probe of the changeset gate" --tags phase2,probe --source-id phase2-probe
printf 'The gate judged this probe.[^phase2-probe]\n\n[^phase2-probe]: phase 2 probe packet\n' >> $ws/ws/references/phase2-probe.md
printf '# Phase 2 probe\n\nA probe packet; nothing is committed.\n' > $ws/probe.md
uv run ai-wiki propose --dir $ws/ws --upload $ws/probe.md --source-id phase2-probe --dry-run --json \
  | jq -c '{status, dry_run, published_revision, errors}'            # would_apply, dry_run true
```

V3. The shadow gate judges the same way (owner token):

```bash
uv run python scripts/dry_run_sample.py -b solvely-wiki-shadow --count 10 > dry-run-shadow.json; echo "exit $?"   # 0
```

V4. A real shadow commit reaches `shadow.git`, then its revert (a first run of the design §9
rollback drill):

```bash
ws=$(mktemp -d); uv run ai-wiki -b solvely-wiki-shadow workspace pull --dir $ws/ws
uv run ai-wiki concept new references/phase2-shadow-smoke.md --dir $ws/ws --type Reference \
  --title "Phase 2 shadow smoke" --description "Probe that the shadow writer commits and reverts" \
  --tags phase2,smoke --source-id phase2-smoke
printf 'The shadow writer committed this probe.[^phase2-smoke]\n\n[^phase2-smoke]: phase 2 smoke packet\n' \
  >> $ws/ws/references/phase2-shadow-smoke.md
printf '# Phase 2 shadow smoke\n\nA probe packet for the shadow writer.\n' > $ws/smoke.md
uv run ai-wiki propose --dir $ws/ws --upload $ws/smoke.md --source-id phase2-smoke --json > smoke.json
jq -c '{id, status, commit, git}' smoke.json                                   # done, pushed true
uv run ai-wiki -b solvely-wiki-shadow admin revert --changeset "$(jq -r .id smoke.json)" \
  --reason "phase 2 smoke" --json | jq -c '{id, status, reverted, commit}'      # done
```

On aliyun-jp:

```bash
as_admin git -C $ORIGIN log --oneline -3                          # revert, the smoke changeset, R0
as_admin git -C $PROD status --porcelain | wc -l                  # 0
as_admin git -C $PROD ls-remote origin refs/heads/main            # production moved only by its own runs
curl -s -H @$P2/legacy.h 'http://127.0.0.1:8788/jobs/pending-audit?older_than_hours=24' | jq -c '{shown, total}'
```

V5. The Phase 1 agent path still works for today's maintainer (eb17): `ai-wiki -b
solvely-wiki jobs --pending-audit` through the public URL answers as in §1, and the next
scheduled production run completes as usual.

When V1–V5 pass: `docker rm ai-wiki-prev-$(cat $P2/mirror-TS)`.

## 11. Hand-over to the shadow agent

The shadow agent itself (prompt, skills, schedule 05:30 and 13:30 CST) is W13's. What this
deployment hands it:

1. Tokens leave the host. The owner token is already in the password manager (§2); the owner
   copies `$P2/shadow-maintainer.token` into the "AI Wiki Maintainer (shadow)" agent's
   Multica custom env as `AIWIKI_TOKEN`. Then on aliyun-jp:
   `shred -u $P2/*.token $P2/owner.h $P2/shadow-maintainer.h $P2/shadow-audit.h`
   (`legacy.h` stays for the rollback checks; the legacy token is in the unit file anyway).
2. On the runtime host (ip-10-2-192-225), with that token:
   `ai-wiki -b solvely-wiki-shadow doctor --role curator` passes every check.
3. The shadow's cursors start from production's latest v4 checkpoint, so both process the
   same window: `ai-wiki -b solvely-wiki-shadow maint import-v4 <checkpoint.json>` (design §9).
4. Record R0 (`cat $P2/R0`): `ai-wiki admin compare --live solvely-wiki --shadow
   solvely-wiki-shadow --since <R0>` compares from it.
5. Enable §8 on the day of the first shadow run.

## 12. Rollback

Each step's rollback is listed with it. Nothing in this runbook changes production's bundle
directory, its clone's refs or its origin, so no rollback restores production data: it
removes what was added, in reverse order.

| Step | Rollback | Needs |
|---|---|---|
| §9 audit timer | disable the timer, remove its units, script, env file, config; `pp remove process:ai-wiki-shadow-audit` + SIGHUP | — |
| §8 watchdog | remove `ai-wiki-watchdog.service.d/phase2-shadow.conf`, daemon-reload | — |
| §7 tunnel | PUT `tunnel-before.json`'s config back | laptop, CF token |
| §6 mirror | §6's rollback block: the live build again, production mount only, no principals; then shadow pull timer off, read clone removed | — |
| §5b shadow mode | remove `phase2-shadow.conf`, daemon-reload, restart | idle |
| §5a principals | remove `phase1-principals.conf`, daemon-reload, restart | idle |
| §4 clone + link | `rm $ROOT/solvely-wiki; rm -rf $SHADOW` | §5b rolled back (`/whoami` check) |
| §3 origin | `rm -rf $ORIGIN` | §4, §6 rolled back |
| §2 principals | remove the file, `/etc/ai-wiki` back to root:root 0700 | §5a, §6 rolled back |

Full rollback (the design's Phase 2 rollback: remove the shadow; production was never
touched). Stop the shadow agent in Multica first, then on the laptop run §7's rollback. Then
on aliyun-jp, four blocks in order; each one that prints `STOP` changed nothing that a rerun
cannot finish, and the next block waits until it has not.

R1. The shadow's audit timer and the watchdog's second bundle:

```bash
systemctl disable --now ai-wiki-shadow-audit.timer
rm -f /etc/systemd/system/ai-wiki-shadow-audit.{service,timer} /usr/local/sbin/ai-wiki-shadow-audit \
      /etc/ai-wiki-shadow-audit.env /etc/systemd/system/ai-wiki-watchdog.service.d/phase2-shadow.conf
systemctl daemon-reload
```

R2. The mirror: run §6's rollback block, until it prints `mirror-rolled-back`. It also stops
the shadow's pull timer and removes its read clone.

R3. The writer, back on the single bundle and the legacy token alone, when idle:

```bash
rm -f /etc/systemd/system/ai-wiki-worker.service.d/phase2-shadow.conf \
      /etc/systemd/system/ai-wiki-worker.service.d/phase1-principals.conf
systemctl daemon-reload
if [ "$(active_jobs $PROD $SHADOW)" = 0 ]; then
  systemctl restart ai-wiki-worker
  for i in $(seq 60); do curl -fsS -o /dev/null -H @$P2/legacy.h http://127.0.0.1:8788/health && break; sleep 2; done
else
  echo 'STOP: the writer has a job; rerun R3 when idle'
fi
curl -s -H @$P2/legacy.h http://127.0.0.1:8788/whoami \
  | jq -c '{principal, writer, bundles, commit: .modes.changesets_commit, principals: .auth.principals}'
#   {"principal":"member:legacy-token","writer":true,"bundles":null,"commit":[],"principals":["member:legacy-token"]}
curl -s -H @$P2/legacy.h http://127.0.0.1:8788/health | jq -c '{bundle, concepts, build}'   # production
```

`member:legacy-token` is the legacy token's id with and without a principals file, so the
principal alone cannot show the restart. `bundles` `null`, `commit` `[]` and the one
principal can: the old process still answers `["solvely-wiki"]`, `["solvely-wiki-shadow"]`
and four principals.

R4. Remove the shadow's state, only once neither service still uses it: a writer still in
multi-bundle mode would find no bundles and answer 503 on every production route, and a
mirror still started with `AIWIKI_PRINCIPALS` could not start again.

```bash
if curl -fsS -H @$P2/legacy.h http://127.0.0.1:8788/whoami | jq -e '.writer and .bundles == null
       and .modes.changesets_commit == [] and .auth.principals == ["member:legacy-token"]' >/dev/null \
   && ! docker inspect ai-wiki --format '{{range .Config.Env}}{{println .}}{{end}}{{range .Mounts}}{{println .Source}}{{end}}' \
        | grep -q -e '^AIWIKI_PRINCIPALS=' -e '^/var/lib/ai-wiki' -e '^/etc/ai-wiki'; then
  # keep the shadow's history for later comparison, then remove it
  tar -C /var/lib -czf $P2/ai-wiki-shadow-$(date -u +%Y%m%dT%H%M%SZ).tgz ai-wiki \
    && rm $ROOT/solvely-wiki && rm -rf $BASE \
    && rm -f /etc/ai-wiki/principals.json && chgrp root /etc/ai-wiki && chmod 0700 /etc/ai-wiki \
    && echo shadow-removed
else
  echo 'STOP: the writer or the mirror still runs the shadow configuration; finish R2 and R3 first'
fi
```

The linked-bundle fix itself needs no rollback: in single-bundle mode it is never reached.

## Open risks

- **One writer process for both bundles.** A shadow the startup recovery cannot reconcile
  keeps the writer from starting, production included. Its origin is local, so remote
  containment is always decidable; if it ever happens, §5b's rollback restores production
  in one restart.
- **Mirror deploys** must carry the live container's mounts and count the shadow's jobs (§6,
  "Deploys from now on"). A deploy that recreates the mirror with only the production mount
  crash-loops it on the missing principals file, a public read outage, and one that counts
  only production's jobs can restart the writer in the middle of a shadow changeset or audit.
- **Codex time.** Shadow audits run at most five a day, at 12:00; production's audits keep
  their own queue order.
- The shadow's read clone lags its origin by up to five minutes, as production's does.
