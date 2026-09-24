#!/usr/bin/env python3
"""Provision the principals file the service loads from AIWIKI_PRINCIPALS (design §8.2).

Stdlib only, apart from the service's own ``aiwiki.service.auth``, whose rules it applies.
On the writer host, run it as root with the writer's venv python (the host python3 is too
old for auth.py) and -B, so no root-owned bytecode lands in admin's tree:

    sudo /home/admin/app/.venv/bin/python -B \\
        /home/admin/app/scripts/provision_principals.py --file /etc/ai-wiki/principals.json list

Commands:
  add PRESET    generate a token for a canonical role (below), store only its sha256 and print
                the token once: it is the only thing written to stdout, so capture it straight
                into the secret store, under `set -o noclobber` so a rerun cannot empty a token
                file it then fails to refill. A token lost before it was stored: remove its id,
                then add it again. --id (same kind: process:, human: or member:), --bundle,
                --limit NAME=N and --expires adjust it.
  add-legacy    register the legacy shared token, read from $AIWIKI_TOKEN (--token-env), as
                member:legacy-token with every scope, as it authenticates without a file; the
                service refuses to start on a file that leaves out a token it is still given.
  list          the principals in force after the next reload, without digests.
  remove ID...  drop principals (incident response, §8.5). Rotate a token with remove + add:
                the service sees neither edit until it reloads.
  check         validate the file through the service's own startup path, including the
                legacy-token invariant when $AIWIKI_TOKEN is set. Exit 0 ok, 1 refused.

$AIWIKI_TOKEN (--token-env) here is the legacy token the services are started with, never a
principal's own aiw_ token (the CLI's $AIWIKI_TOKEN): that is refused. Check with each
service's own, since a bare `check` cannot see what they are given:

    AIWIKI_TOKEN="$(systemctl show -p Environment --value ai-wiki-worker | tr ' ' '\\n' \\
        | sed -n 's/^AIWIKI_TOKEN=//p')" provision_principals.py check
    AIWIKI_TOKEN="$(docker inspect ai-wiki --format '{{range .Config.Env}}{{println .}}{{end}}' \\
        | sed -n 's/^AIWIKI_TOKEN=//p')" provision_principals.py check

Presets (the id is also the actor stamped into generated.by / verified[].by; a member never is):
  owner              human:guobaoqi                     aiw_h_  every scope, every bundle
  maintainer         process:ai-wiki-maintainer         aiw_c_  read, submit, curate
                     solvely-wiki and solvely-wiki-shadow; 30/h, 150/day, 10 deprecations/day
  shadow-maintainer  process:ai-wiki-maintainer-shadow  aiw_c_  as maintainer, solvely-wiki-shadow only
  auditor            process:ai-wiki-auditor            aiw_a_  read, audit; both bundles; 200 reviews/day
                     phase 4a: not before the Auditor agent runs as its own OS user (D2)
  watchdog           process:ai-wiki-watchdog           aiw_r_  read
  member             member:<name> (--id required)      aiw_m_  read, submit

Every edit is validated with auth.parse before it is written, so whatever the file itself
makes the service refuse at startup (a process holding curate and audit, admin or
human_verify; a duplicate id or digest; an unknown limit) is refused here and the file is
left as it was. The one startup rule that depends on the services' environment is only
warned about: while they are started with AIWIKI_TOKEN, a principal must hold its sha256.
`add` and `remove` warn when an edit leaves the legacy token without one (§8.5 must be able
to drop it); then drop AIWIKI_TOKEN from ai-wiki-worker's unit and the ai-wiki container
before either restarts, or neither starts again.

The file is replaced atomically (the mirror mounts the directory, never the file) with mode
0640, keeping its owner and group; a new file takes its directory's group, so keep
/etc/ai-wiki at root:<writer user> 0750.

After an edit, `check` it, then reload each service and confirm in an admin's GET /whoami
("auth"): a refused reload only logs. SIGHUP reloads only a service started with
AIWIKI_PRINCIPALS; one started without it exits on SIGHUP, so adopting the file takes a
restart. Signal the server process, never the unit's cgroup:

    kill -HUP "$(pgrep -P "$(systemctl show -p MainPID --value ai-wiki-worker)" -f aiwiki.service)"
    docker exec ai-wiki uv run --no-dev python -m aiwiki.service.auth /etc/ai-wiki/principals.json
    docker kill -s HUP ai-wiki
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

from aiwiki.service import auth

DEFAULT_FILE = "/etc/ai-wiki/principals.json"
_MAINTAINER_LIMITS = {"changesets_per_hour": 30, "changesets_per_day": 150, "deprecations_per_day": 10}
# Scopes come from auth.ROLES, so `ai-wiki doctor --role` finds exactly the role's scopes.
PRESETS: dict[str, dict] = {
    "owner": {"id": "human:guobaoqi", "prefix": "aiw_h_", "scopes": sorted(auth.SCOPES)},
    "maintainer": {"id": "process:ai-wiki-maintainer", "prefix": "aiw_c_", "scopes": sorted(auth.ROLES["curator"]),
                   "bundles": ["solvely-wiki", "solvely-wiki-shadow"], "limits": _MAINTAINER_LIMITS},
    "shadow-maintainer": {"id": "process:ai-wiki-maintainer-shadow", "prefix": "aiw_c_",
                          "scopes": sorted(auth.ROLES["curator"]), "bundles": ["solvely-wiki-shadow"],
                          "limits": _MAINTAINER_LIMITS},
    "auditor": {"id": "process:ai-wiki-auditor", "prefix": "aiw_a_", "scopes": sorted(auth.ROLES["auditor"]),
                "bundles": ["solvely-wiki", "solvely-wiki-shadow"], "limits": {"reviews_per_day": 200}},
    "watchdog": {"id": "process:ai-wiki-watchdog", "prefix": "aiw_r_", "scopes": sorted(auth.ROLES["reader"])},
    "member": {"id": None, "prefix": "aiw_m_", "scopes": sorted(auth.ROLES["member"])},
}


def _read(path: Path, *, create: bool = False) -> dict:
    """The raw document, so an edit can also drop an entry the service would refuse."""
    if create and not path.exists():
        return {"principals": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise auth.PrincipalsError(f"cannot read principals file {path}: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("principals"), list):
        raise auth.PrincipalsError('principals file must be {"principals": [...]}')
    return data


def _write(path: Path, data: dict) -> None:
    auth.parse(data)  # whatever the file makes the service refuse at startup is refused before a byte is written
    old = path.stat() if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        new = os.stat(tmp)
        owner = (old.st_uid, old.st_gid) if old else (new.st_uid, path.parent.stat().st_gid)
        if owner != (new.st_uid, new.st_gid):
            os.chown(tmp, *owner)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def add(path: Path, preset: str, *, pid: str | None = None, bundles: list[str] | None = None,
        limits: dict[str, int] | None = None, expires: str | None = None) -> str:
    """Add a principal from `preset`; return its new token, whose sha256 alone is stored."""
    spec = PRESETS[preset]
    # The kind decides the actor: an --id of another kind would stamp (or not stamp) the wrong one.
    kind = (spec["id"] or "member:").partition(":")[0]
    pid = pid or spec["id"] or ""
    if not pid.startswith(f"{kind}:"):
        raise auth.PrincipalsError(f"preset {preset} needs --id {kind}:<name>")
    token = spec["prefix"] + secrets.token_urlsafe(32)
    entry = {"id": pid, "token_sha256": auth.token_sha256(token), "prefix": spec["prefix"],
             "scopes": list(spec["scopes"])}
    if bundles or spec.get("bundles"):
        entry["bundles"] = list(bundles or spec["bundles"])
    limits = {**spec.get("limits", {}), **(limits or {})}
    if limits:
        entry["limits"] = limits
    if expires:
        entry["expires"] = expires
    data = _read(path, create=True)
    data["principals"].append(entry)
    _write(path, data)
    return token


def add_legacy(path: Path, token: str | None, *, expires: str | None = None) -> None:
    """Register the legacy shared token as auth.LEGACY_ID with every scope, as it works without a file."""
    if not token:
        raise auth.PrincipalsError("the legacy token's environment variable is empty or unset")
    entry = {"id": auth.LEGACY_ID, "token_sha256": auth.token_sha256(token), "scopes": sorted(auth.SCOPES)}
    if expires:
        entry["expires"] = expires
    data = _read(path, create=True)
    data["principals"].append(entry)
    _write(path, data)


def listing(path: Path) -> list[dict]:
    """What each principal is, as /whoami reports it, without its digest."""
    return [{"id": p.id, "role": p.role, "actor": p.actor, "prefix": p.prefix, "scopes": sorted(p.scopes),
             "bundles": sorted(p.bundles) if p.bundles is not None else None, "limits": dict(p.limits),
             "expires": p.expires.isoformat() if p.expires else None} for p in auth.load(path)]


def remove(path: Path, *pids: str) -> None:
    """Drop principals; only the result is validated, so one call can drop every entry the service refuses."""
    data = _read(path)
    present = [entry.get("id") for entry in data["principals"] if isinstance(entry, dict)]
    missing = [pid for pid in pids if pid not in present]
    if missing:
        raise auth.PrincipalsError(f"{', '.join(missing)}: no such principal in {path}")
    kept = [entry for entry in data["principals"] if not (isinstance(entry, dict) and entry.get("id") in pids)]
    _write(path, {**data, "principals": kept})


def check(path: Path, token: str | None) -> str:
    """Load `path` as the service starts on it; return what an operator needs to see."""
    env = {"AIWIKI_PRINCIPALS": str(path), **({"AIWIKI_TOKEN": token} if token else {})}
    principals = auth.Registry.from_env(env).principals
    line = f"ok: {len(principals)} principals: {', '.join(p.id for p in principals)}"
    if not token:
        return line + ("\nlegacy token not given: the AIWIKI_TOKEN startup invariant was not checked; "
                       "check again with each service's own (see --help)")
    holder = next(p.id for p in principals if p.token_sha256 == auth.token_sha256(token))
    return line + f"\nlegacy token: held by {holder}"


def legacy_token(value: str | None, name: str = "AIWIKI_TOKEN") -> str | None:
    """The legacy token the services are started with, as read from $name; never a principal's own."""
    if value and value.startswith("aiw_"):
        raise auth.PrincipalsError(f"${name} holds a principal's own aiw_ token (the CLI's), not the legacy token the "
                                   "services are started with: unset it, or set it to the services' (see --help)")
    return value or None


def unheld_warning(path: Path, token: str | None, removed: tuple[str, ...] = ()) -> str | None:
    """Why the services, still started with the legacy token, would refuse the edited file at their next start."""
    if token:
        if any(p.token_sha256 == auth.token_sha256(token) for p in auth.load(path)):
            return None
    elif auth.LEGACY_ID not in removed:
        return None
    return (f"warning: no principal in {path} holds the legacy token any more, so ai-wiki-worker (AIWIKI_TOKEN in its "
            "unit) and the ai-wiki container, which are still started with it, will refuse to start on this file. "
            "Drop AIWIKI_TOKEN from both before either restarts (recreate the container without it), or add-legacy.")


def _limit(value: str) -> tuple[str, int]:
    name, _, number = value.partition("=")
    try:
        return name, int(number)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected NAME=N, got {value!r}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=Path(os.environ.get("AIWIKI_PRINCIPALS") or DEFAULT_FILE),
                        help=f"principals file (default: $AIWIKI_PRINCIPALS or {DEFAULT_FILE})")
    legacy = argparse.ArgumentParser(add_help=False)
    legacy.add_argument("--token-env", default="AIWIKI_TOKEN", metavar="VAR",
                        help="variable holding the legacy token the services are started with (default: AIWIKI_TOKEN)")
    commands = parser.add_subparsers(dest="command", required=True)
    p_add = commands.add_parser("add", parents=[legacy], help="add a principal from a preset; print its token once")
    p_add.add_argument("preset", choices=PRESETS)
    p_add.add_argument("--id", dest="pid", help="principal id instead of the preset's (required for member)")
    p_add.add_argument("--bundle", action="append", dest="bundles", help="bundle it may touch, instead of "
                       "the preset's (repeatable)")
    p_add.add_argument("--limit", action="append", type=_limit, default=[], metavar="NAME=N",
                       help="override one of the preset's limits (repeatable)")
    p_add.add_argument("--expires", metavar="YYYY-MM-DD")
    p_legacy = commands.add_parser("add-legacy", parents=[legacy],
                                   help="register the legacy shared token as member:legacy-token")
    p_legacy.add_argument("--expires", metavar="YYYY-MM-DD")
    commands.add_parser("list", help="show the principals without digests")
    p_remove = commands.add_parser("remove", parents=[legacy], help="drop principals")
    p_remove.add_argument("ids", nargs="+", metavar="ID")
    commands.add_parser("check", parents=[legacy], help="validate the file as the service loads it at startup")
    args = parser.parse_args(argv)

    path = args.file.expanduser()
    warning = None
    try:
        # Read before any edit, so a principal's own token in $AIWIKI_TOKEN refuses the command, not half of it.
        token = legacy_token(os.environ.get(args.token_env), args.token_env) if args.command != "list" else None
        if args.command == "add":
            new = add(path, args.preset, pid=args.pid, bundles=args.bundles, limits=dict(args.limit),
                      expires=args.expires)
            print(new)
            print(f"added {args.pid or PRESETS[args.preset]['id']} to {path}; the token above is shown once and "
                  "stored only as its sha256. Check the file, then reload (see --help).", file=sys.stderr)
            warning = unheld_warning(path, token)
        elif args.command == "add-legacy":
            add_legacy(path, token, expires=args.expires)
            print(f"added {auth.LEGACY_ID} (sha256 of ${args.token_env}, every scope) to {path}", file=sys.stderr)
        elif args.command == "list":
            print(json.dumps(listing(path), indent=2))
        elif args.command == "remove":
            remove(path, *args.ids)
            print(f"removed {', '.join(args.ids)} from {path}; each authenticates until the services reload "
                  "(see --help)", file=sys.stderr)
            warning = unheld_warning(path, token, tuple(args.ids))
        else:
            print(check(path, token))
    except (auth.PrincipalsError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if warning:
        print(warning, file=sys.stderr)
    return 0

if __name__ == "__main__":
    sys.exit(main())
