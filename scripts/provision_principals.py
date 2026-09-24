#!/usr/bin/env python3
"""Provision the principals file the service loads from AIWIKI_PRINCIPALS (design §8.2).

Stdlib only, apart from the service's own ``aiwiki.service.auth``, whose rules it applies.
On the writer host, run it as root with the writer's venv python (the host python3 is too
old for auth.py), so no bytecode lands in admin's tree:

    sudo env PYTHONDONTWRITEBYTECODE=1 /home/admin/app/.venv/bin/python \\
        /home/admin/app/scripts/provision_principals.py --file /etc/ai-wiki/principals.json list

Commands:
  add PRESET    generate a token for a canonical role (below), store only its sha256 and print
                the token once: it is the only thing written to stdout, so capture it straight
                into the secret store. --id, --bundle, --limit NAME=N and --expires adjust it.
  add-legacy    register the legacy shared token, read from $AIWIKI_TOKEN (--token-env), as
                member:legacy-token with every scope, as it authenticates without a file; the
                service refuses to start on a file that leaves out a token it is still given.
  list          the principals in force after the next reload, without digests.
  remove ID     drop a principal (incident response, §8.5). Rotate a token with remove + add:
                the service sees neither edit until it reloads.
  check         validate the file through the service's own startup path, including the
                legacy-token invariant when $AIWIKI_TOKEN is set. Exit 0 ok, 1 refused.

Presets (the id is also the actor stamped into generated.by / verified[].by; a member never is):
  owner              human:guobaoqi                     aiw_h_  every scope, every bundle
  maintainer         process:ai-wiki-maintainer         aiw_c_  read, submit, curate
                     solvely-wiki and solvely-wiki-shadow; 30/h, 150/day, 10 deprecations/day
  shadow-maintainer  process:ai-wiki-maintainer-shadow  aiw_c_  as maintainer, solvely-wiki-shadow only
  auditor            process:ai-wiki-auditor            aiw_a_  read, audit; both bundles; 200 reviews/day
                     phase 4a: not before the Auditor agent runs as its own OS user (D2)
  watchdog           process:ai-wiki-watchdog           aiw_r_  read
  member             member:<name> (--id required)      aiw_m_  read, submit

Every edit is validated with auth.parse before it is written, so whatever the service would
refuse at startup (a process holding curate and audit, admin or human_verify; a duplicate id
or digest; an unknown limit) is refused here and the file is left as it was. The file is
replaced atomically (the mirror mounts the directory, never the file) with mode 0640, keeping
its owner and group; a new file takes its directory's group, so keep /etc/ai-wiki at
root:<writer user> 0750.

After an edit, `check` it, then reload each service and confirm in an admin's GET /whoami
("auth"): a refused reload only logs. Signal the server process, never the unit's cgroup:

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
    auth.parse(data)  # anything the service would refuse at startup is refused before a byte is written
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
    pid = pid or spec["id"]
    if pid is None:
        raise auth.PrincipalsError(f"preset {preset} needs --id member:<name>")
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


def remove(path: Path, pid: str) -> None:
    data = _read(path)
    kept = [entry for entry in data["principals"] if not (isinstance(entry, dict) and entry.get("id") == pid)]
    if len(kept) == len(data["principals"]):
        raise auth.PrincipalsError(f"{pid}: no such principal in {path}")
    _write(path, {**data, "principals": kept})


def check(path: Path, token: str | None) -> str:
    """Load `path` as the service starts on it; return what an operator needs to see."""
    env = {"AIWIKI_PRINCIPALS": str(path), **({"AIWIKI_TOKEN": token} if token else {})}
    principals = auth.Registry.from_env(env).principals
    line = f"ok: {len(principals)} principals: {', '.join(p.id for p in principals)}"
    if not token:
        return line + "\nlegacy token not given: the AIWIKI_TOKEN startup invariant was not checked"
    holder = next(p.id for p in principals if p.token_sha256 == auth.token_sha256(token))
    return line + f"\nlegacy token: held by {holder}"


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
    commands = parser.add_subparsers(dest="command", required=True)
    p_add = commands.add_parser("add", help="add a principal from a preset; print its token once")
    p_add.add_argument("preset", choices=PRESETS)
    p_add.add_argument("--id", dest="pid", help="principal id instead of the preset's (required for member)")
    p_add.add_argument("--bundle", action="append", dest="bundles", help="bundle it may touch, instead of "
                       "the preset's (repeatable)")
    p_add.add_argument("--limit", action="append", type=_limit, default=[], metavar="NAME=N",
                       help="override one of the preset's limits (repeatable)")
    p_add.add_argument("--expires", metavar="YYYY-MM-DD")
    p_legacy = commands.add_parser("add-legacy", help="register the legacy shared token as member:legacy-token")
    p_legacy.add_argument("--token-env", default="AIWIKI_TOKEN", help="variable holding it (default: AIWIKI_TOKEN)")
    p_legacy.add_argument("--expires", metavar="YYYY-MM-DD")
    commands.add_parser("list", help="show the principals without digests")
    p_remove = commands.add_parser("remove", help="drop a principal")
    p_remove.add_argument("id")
    p_check = commands.add_parser("check", help="validate the file as the service loads it at startup")
    p_check.add_argument("--token-env", default="AIWIKI_TOKEN",
                         help="variable holding the legacy token the service is still given (default: AIWIKI_TOKEN)")
    args = parser.parse_args(argv)

    path = args.file.expanduser()
    try:
        if args.command == "add":
            token = add(path, args.preset, pid=args.pid, bundles=args.bundles, limits=dict(args.limit),
                        expires=args.expires)
            print(token)
            print(f"added {args.pid or PRESETS[args.preset]['id']} to {path}; the token above is shown once and "
                  "stored only as its sha256. Check the file, then reload (see --help).", file=sys.stderr)
        elif args.command == "add-legacy":
            add_legacy(path, os.environ.get(args.token_env), expires=args.expires)
            print(f"added {auth.LEGACY_ID} (sha256 of ${args.token_env}, every scope) to {path}", file=sys.stderr)
        elif args.command == "list":
            print(json.dumps(listing(path), indent=2))
        elif args.command == "remove":
            remove(path, args.id)
            print(f"removed {args.id} from {path}; it authenticates until the services reload (see --help)",
                  file=sys.stderr)
        else:
            print(check(path, os.environ.get(args.token_env)))
    except (auth.PrincipalsError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
