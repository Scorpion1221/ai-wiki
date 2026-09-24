"""Principals: which identity a bearer token is, its scopes, and the bundles it may touch.

``AIWIKI_PRINCIPALS`` names a JSON file (``0640 root:<writer user>``; the read mirror
mounts it read-only) that stores only sha256 digests of the tokens:

    {"principals": [
      {"id": "process:ai-wiki-maintainer", "token_sha256": "<64 hex>", "prefix": "aiw_c_",
       "scopes": ["read", "submit", "curate"], "bundles": ["solvely-wiki"],
       "limits": {"changesets_per_day": 150}, "expires": "2026-12-31"}]}

``bundles`` omitted means every bundle. Without the file, the legacy shared
``AIWIKI_TOKEN`` authenticates as one principal holding every scope. With the file,
``AIWIKI_TOKEN`` is only honoured through a principal that holds its digest (phase 1 keeps
eb17 as ``member:legacy-eb17``), so startup refuses a file that leaves it out rather than
answer its holders 401. An invalid file stops startup; a SIGHUP re-reads it, and an invalid
file on reload keeps the principals in force. A reload may drop the legacy token's principal
(incident response, §8.5); the next start then asks for AIWIKI_TOKEN to be unset.
So check an edited file first (``python -m aiwiki.service.auth <file>``) and confirm the
reload in an admin's ``GET /whoami``. Signal only the server process (``kill -HUP $MAINPID``):
``systemctl kill`` signals the whole cgroup by default, killing any running curation agent.
No FastAPI import, so this stays usable without the service extra.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

SCOPES = frozenset({"read", "submit", "curate", "audit", "human_verify", "admin"})
# Exact scope sets of the agent roles (`ai-wiki doctor --role` fails on missing or extra scopes).
ROLES = {
    "curator": frozenset({"read", "submit", "curate"}),
    "auditor": frozenset({"read", "audit"}),
    "member": frozenset({"read", "submit"}),
    "reader": frozenset({"read"}),
}
# Quota names (design §2.2, §8.2); a misspelt one is refused, or its quota would never apply.
LIMITS = frozenset({"changesets_per_hour", "changesets_per_day", "deprecations_per_day", "reviews_per_day"})
LEGACY_ID = "member:legacy-token"
_ID = re.compile(r"(process|human|member):[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEYS = {"id", "token_sha256", "prefix", "scopes", "bundles", "limits", "expires"}
_log = logging.getLogger(__name__)


class PrincipalsError(RuntimeError):
    """The principals configuration is unusable; the service must not start on it."""


class AuthError(Exception):
    """A request is not authenticated (401) or not authorized (403)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    id: str
    token_sha256: str
    scopes: frozenset[str]
    bundles: frozenset[str] | None = None
    limits: Mapping[str, int] = field(default_factory=dict, hash=False)
    prefix: str | None = None
    expires: date | None = None

    def __post_init__(self) -> None:
        # Read-only, so no request handler can rewrite the quota in force until the next reload.
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))

    @property
    def actor(self) -> str | None:
        """The id stamped into `generated.by`/`verified[].by`; a member is never written there."""
        return self.id if self.id.startswith(("process:", "human:")) else None

    @property
    def role(self) -> str:
        for name, scopes in ROLES.items():
            if self.scopes == scopes:
                return name
        return "admin" if "admin" in self.scopes else "custom"

    def allows(self, bundle: str) -> bool:
        return self.bundles is None or bundle in self.bundles


def _principal(entry: object, index: int) -> Principal:
    if not isinstance(entry, dict):
        raise PrincipalsError(f"principals[{index}] must be an object")
    pid = entry.get("id")
    if not isinstance(pid, str) or not _ID.fullmatch(pid):
        raise PrincipalsError(f"principals[{index}].id must be process:<name>, human:<name> or member:<name>")
    unknown = sorted(set(entry) - _KEYS)
    if unknown:
        raise PrincipalsError(f"{pid}: unknown keys {', '.join(unknown)}")
    digest = entry.get("token_sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest.lower()):
        raise PrincipalsError(f"{pid}: token_sha256 must be 64 hex characters")
    scopes = entry.get("scopes")
    if not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) for s in scopes):
        raise PrincipalsError(f"{pid}: scopes must be a non-empty list")
    if set(scopes) - SCOPES:
        raise PrincipalsError(f"{pid}: unknown scopes {', '.join(sorted(set(scopes) - SCOPES))}")
    # An agent must not verify its own output (design §5.4 A1): a process cannot both curate and
    # audit, nor hold admin (which submits either changeset kind) or human_verify (§5.5).
    if pid.startswith("process:"):
        if {"curate", "audit"} <= set(scopes):
            raise PrincipalsError(f"{pid}: a process principal must not hold both curate and audit")
        if {"human_verify", "admin"} & set(scopes):
            held = ", ".join(sorted({"human_verify", "admin"} & set(scopes)))
            raise PrincipalsError(f"{pid}: a process principal must not hold {held}")
    bundles = entry.get("bundles")
    if bundles is not None and (not isinstance(bundles, list)
                                or not all(isinstance(b, str) and b for b in bundles)):
        raise PrincipalsError(f"{pid}: bundles must be a list of bundle names")
    limits = entry.get("limits", {})
    if not isinstance(limits, dict) or not all(type(v) is int and v >= 0 for v in limits.values()):
        raise PrincipalsError(f"{pid}: limits must map names to non-negative integers")
    if set(limits) - LIMITS:
        raise PrincipalsError(f"{pid}: unknown limits {', '.join(sorted(set(limits) - LIMITS))}")
    prefix = entry.get("prefix")
    if prefix is not None and not isinstance(prefix, str):
        raise PrincipalsError(f"{pid}: prefix must be a string")
    expires = entry.get("expires")
    if expires is not None:
        try:
            expires = date.fromisoformat(expires)
        except (TypeError, ValueError):
            raise PrincipalsError(f"{pid}: expires must be a YYYY-MM-DD date") from None
    return Principal(pid, digest.lower(), frozenset(scopes),
                     frozenset(bundles) if bundles is not None else None, limits, prefix, expires)


def parse(data: object) -> tuple[Principal, ...]:
    """Validate a principals document; any problem refuses the whole file."""
    if not isinstance(data, dict) or set(data) != {"principals"} or not isinstance(data["principals"], list):
        raise PrincipalsError('principals file must be {"principals": [...]}')
    principals: list[Principal] = []
    for index, entry in enumerate(data["principals"]):
        principal = _principal(entry, index)
        if any(p.id == principal.id for p in principals):
            raise PrincipalsError(f"{principal.id}: duplicate principal id")
        if any(p.token_sha256 == principal.token_sha256 for p in principals):
            raise PrincipalsError(f"{principal.id}: token_sha256 is shared with another principal")
        principals.append(principal)
    return tuple(principals)


def load(path: Path) -> tuple[Principal, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PrincipalsError(f"cannot read principals file {path}: {exc}") from None
    return parse(data)


class Registry:
    """The principals in force. `reload()` swaps the whole tuple, so readers need no lock."""

    def __init__(self, principals: tuple[Principal, ...], path: Path | None = None):
        self.principals = principals
        self.path = path
        self.loaded_at = datetime.now(UTC)
        self.reload_error: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Registry:
        token = environ.get("AIWIKI_TOKEN")
        if environ.get("AIWIKI_PRINCIPALS"):
            path = Path(environ["AIWIKI_PRINCIPALS"]).expanduser()
            principals = load(path)
            if token and not any(hmac.compare_digest(token_sha256(token), p.token_sha256) for p in principals):
                raise PrincipalsError(
                    f"AIWIKI_TOKEN is set, but no principal in {path} holds its sha256, so its holders would get "
                    "401: add the legacy token's principal (e.g. member:legacy-eb17) or unset AIWIKI_TOKEN")
            return cls(principals, path)
        if not token:
            raise PrincipalsError("set AIWIKI_PRINCIPALS (principals file) or AIWIKI_TOKEN (legacy shared token)")
        return cls((Principal(LEGACY_ID, token_sha256(token), SCOPES),))

    def authorize(self, authorization: str | None, *scopes: str, today: date | None = None) -> Principal:
        """The principal behind `Bearer <token>`, holding any of `scopes` (none: any valid token)."""
        token = authorization[len("Bearer "):] if authorization and authorization.startswith("Bearer ") else ""
        digest = token_sha256(token)
        match = None
        for principal in self.principals:  # constant-time compare against every entry
            if hmac.compare_digest(digest, principal.token_sha256):
                match = principal
        if not token or match is None:
            raise AuthError(401, "invalid or missing bearer token")
        if match.expires is not None and (today or datetime.now(UTC).date()) > match.expires:
            raise AuthError(401, "bearer token expired")
        if scopes and match.scopes.isdisjoint(scopes):
            raise AuthError(403, f"principal {match.id} lacks scope {' or '.join(scopes)}")
        return match

    def reload(self) -> bool:
        """Re-read the principals file; an invalid file keeps the principals already in force."""
        if self.path is None:
            return False
        try:
            principals = load(self.path)
        except PrincipalsError as exc:
            self.reload_error = str(exc)
            _log.error("principals reload refused, keeping %d principals in force: %s", len(self.principals), exc)
            return False
        self.principals, self.loaded_at, self.reload_error = principals, datetime.now(UTC), None
        _log.warning("principals reloaded from %s: %d principals", self.path, len(principals))
        return True

    def receipt(self) -> dict:
        """What is in force, so an operator can confirm a reload took (a refused one only logs)."""
        return {"principals": [p.id for p in self.principals],
                "loaded_at": self.loaded_at.isoformat(timespec="seconds"), "reload_error": self.reload_error}


@contextmanager
def reload_on_sighup(registry: Registry) -> Iterator[None]:
    """Reload the principals file on SIGHUP while the server runs (only the main thread can)."""
    if registry.path is None or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGHUP, lambda _signum, _frame: registry.reload())
    try:
        yield
    finally:
        signal.signal(signal.SIGHUP, previous)


def main(argv: list[str] | None = None) -> int:
    """`python -m aiwiki.service.auth <file>`: validate a principals file before sending SIGHUP."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m aiwiki.service.auth <principals.json>", file=sys.stderr)
        return 2
    try:
        principals = load(Path(args[0]))
    except PrincipalsError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"ok: {len(principals)} principals: {', '.join(p.id for p in principals)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
