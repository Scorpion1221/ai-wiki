"""Server-owned maintenance state: run leases, collector cursors and the work-item queue.

Everything lives under ``<bundle>/.okf/maint/`` (Git-ignored operational state). Every
record is written with fsync + atomic rename, so a crash leaves the old or the new record:

    items/<id>/item.json         one work item and its state machine
    items/<id>/files/<sha256>    frozen evidence bytes, content-addressed (never rewritten)
    cursors/{repos,issues}.json  collector cursors, updated by compare-and-swap on ``etag``
    lease-{maintainer,auditor}.json  one run-level lease per role

Items move ready -> in_progress(run) -> curated | skipped | duplicate | split |
needs_access | needs_conversion | needs_human, or -> parked, which returns to ready when the
next maintainer run takes the lease. Attempt caps and non-retryable failures move an item to
needs_human; that only raises an alert and never blocks any other item.

The in-place Codex audit can write ``.okf``, so nothing read back from disk is trusted: an
item.json must be well formed and name its own directory, every evidence blob is re-hashed
on read, and a lease file can never outlive one TTL past its last renewal.

Deterministic and stdlib only: nothing here runs an LLM.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiwiki.version import build, service_identity

from ..runtime.failure import CLASSES, redact
from . import ingest as I

LEASE_TTL = timedelta(hours=3)
CLOCK_SKEW = timedelta(minutes=5)  # a lease renewed further in the future than this is forged
ROLES = {"maintainer": "curate", "auditor": "audit"}  # lease role -> scope it requires
CURSORS = ("repos", "issues")
# Park classes: the P0 failure taxonomy plus "context" (the agent ran out of context).
ATTEMPT_CLASSES = frozenset(CLASSES) | {"context"}
PARK_CLASSES = ATTEMPT_CLASSES - {"interrupted"}  # interrupted is the server's own sweep
COUNTED = frozenset({"model_output", "context", "internal"})
NOT_RETRYABLE = frozenset(cls for cls, (retryable, _after) in CLASSES.items() if not retryable)
MAX_COUNTED = 3
MAX_STARTED = 8
AGING_PER_DAY = 5
DEFAULT_PRIORITY = 40
TERMINAL = frozenset({"curated", "skipped", "duplicate", "split", "needs_access", "needs_conversion",
                      "needs_human"})
SKIP_REASONS = frozenset({"no_durable_knowledge", "insufficient_evidence", "out_of_scope"})
AGENT_OUTCOMES = frozenset({"skipped", "duplicate", "needs_access", "needs_conversion", "parked", "split"})
MERGEABLE = frozenset({"ready", "parked"})  # not yet claimed again: newer evidence folds in
ADMIN_OUTCOMES = frozenset({"skipped", "duplicate", "needs_access", "needs_conversion"})
REOPENABLE = frozenset({"needs_human", "skipped", "duplicate", "needs_access", "needs_conversion", "parked"})
MAX_FILES = 64
MAX_CHILDREN = 20
_ID = re.compile(r"it_[0-9a-f]{12}")
_SHA = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RUN = re.compile(r"[\w.:@/-]{1,128}")
_FIELDS = {"id": str, "status": str, "origin": dict, "topic_key": str, "item_key": str, "priority": int,
           "brief": str, "files": list, "attempts": dict, "versions": list, "created_at": str}
_LOCK = threading.Lock()


class MaintError(Exception):
    """A rejected request: HTTP status, a stable ``code`` and extra response fields."""

    def __init__(self, status: int, code: str, message: str, /, **extra):
        super().__init__(message)
        self.status, self.code, self.extra = status, code, extra

    def detail(self) -> dict:
        return {"code": self.code, "message": str(self), **self.extra}


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _age_s(value: object, now: datetime) -> int | None:
    parsed = _parse(value)
    return int((now - parsed).total_seconds()) if parsed else None


# --- durable records ---------------------------------------------------------------

def _atomic_write(path: Path, data: bytes) -> None:
    """fsync a sibling temp file, then rename it over ``path``; never leave a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: dict) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _root(bundle: Path) -> Path:
    return bundle / ".okf" / "maint"


def _item_dir(bundle: Path, item_id: str) -> Path:
    return _root(bundle) / "items" / item_id


def _lease_path(bundle: Path, role: str) -> Path:
    return _root(bundle) / f"lease-{role}.json"


def _cursor_path(bundle: Path, name: str) -> Path:
    return _root(bundle) / "cursors" / f"{name}.json"


def _valid(item: object, item_id: str) -> bool:
    """The shape every reader relies on; its id is the directory it lives in, so no field
    read back from disk can steer a path outside that directory."""
    if not isinstance(item, dict) or item.get("id") != item_id or not all(
            isinstance(item.get(key), kind) for key, kind in _FIELDS.items()):
        return False
    attempts = item["attempts"]
    return (isinstance(item["origin"].get("kind"), str)
            and all(isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("bytes"), int)
                    and isinstance(f.get("sha256"), str) and _SHA.fullmatch(f["sha256"]) for f in item["files"])
            and all(isinstance(v, dict) for v in item["versions"])
            and isinstance(attempts.get("started"), int) and isinstance(attempts.get("counted"), int)
            and isinstance(attempts.get("history"), list) and all(isinstance(h, dict) for h in attempts["history"])
            and isinstance(item.get("resolution") or {}, dict) and isinstance(item.get("reopened", []), list))


def _read_item(bundle: Path, item_id: str) -> dict | None:
    """A committed, well-formed item; None when it is missing, half-created or corrupt."""
    if not _ID.fullmatch(item_id):
        return None
    item = _read_json(_item_dir(bundle, item_id) / "item.json")
    return item if _valid(item, item_id) else None


def _scan(bundle: Path) -> tuple[list[dict], list[str]]:
    """Every committed item, and the ids whose item.json is corrupt (skipped, never fatal)."""
    base = _root(bundle) / "items"
    items: list[dict] = []
    corrupt: list[str] = []
    for path in sorted(base.iterdir()) if base.is_dir() else ():
        if not _ID.fullmatch(path.name) or not (path / "item.json").exists():
            continue  # a directory without item.json is an interrupted create
        item = _read_item(bundle, path.name)
        if item is None:
            corrupt.append(path.name)
        else:
            items.append(item)
    return items, corrupt


def _items(bundle: Path) -> list[dict]:
    return _scan(bundle)[0]


def _load(bundle: Path, item_id: str) -> dict:
    item = _read_item(bundle, item_id)
    if item is None:
        if _ID.fullmatch(item_id) and (_item_dir(bundle, item_id) / "item.json").exists():
            raise MaintError(500, "item_corrupt", f"{item_id}: item.json is malformed; restore or remove it")
        raise MaintError(404, "not_found", f"no such item: {item_id}")
    return item


def _save(bundle: Path, item: dict, now: datetime) -> None:
    item["updated_at"] = _iso(now)
    _write_json(_item_dir(bundle, item["id"]) / "item.json", item)


# --- validation ----------------------------------------------------------------------

def item_key(collector: str, topic_key: str, shas) -> str:
    """Idempotency key of one collected item: sha256(collector|topic_key|sorted file shas)."""
    return hashlib.sha256("|".join([collector, topic_key, *sorted(shas)]).encode("utf-8")).hexdigest()


def _text(value: object, field: str, limit: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise MaintError(400, "input", f"{field} must be a {'nonempty ' if required else ''}string "
                                       f"of at most {limit} characters")
    return value


def _check_run(run: object) -> str:
    if not isinstance(run, str) or not _RUN.fullmatch(run):
        raise MaintError(400, "input", "X-AIWiki-Run must name the run (1-128 of A-Z a-z 0-9 _ . : @ / -)")
    return run


def _fits(files: list[dict]) -> bool:
    return len(files) <= MAX_FILES and sum(f["bytes"] for f in files) <= I.MAX_BYTES


def _check_size(files: list[dict]) -> None:
    if not _fits(files):
        raise MaintError(413, "too_large", f"an item holds at most {MAX_FILES} files and {I.MAX_BYTES} bytes "
                                           "of evidence")


def _decode_files(files: object) -> list[tuple[dict, bytes]]:
    """Evidence bytes are required: ``[{name, content_b64, origin}]`` with unique safe names."""
    if not isinstance(files, list) or not files:
        raise MaintError(400, "input", "files must be a nonempty list; items carry their evidence bytes")
    decoded: list[tuple[dict, bytes]] = []
    for raw in files:
        name = raw.get("name") if isinstance(raw, dict) else None
        if not isinstance(name, str) or not _NAME.fullmatch(name) or any(m["name"] == name for m, _ in decoded):
            raise MaintError(400, "input", f"invalid or duplicate file name: {name!r}")
        try:
            data = base64.b64decode(raw.get("content_b64"), validate=True)
        except (TypeError, ValueError, binascii.Error):
            raise MaintError(400, "input", f"{name}: content_b64 is not valid base64") from None
        if not data:
            raise MaintError(400, "input", f"{name}: empty evidence")
        origin = raw.get("origin", {})
        if not isinstance(origin, dict):
            raise MaintError(400, "input", f"{name}: origin must be an object")
        meta = {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "origin": origin}
        decoded.append((meta, data))
    _check_size([meta for meta, _ in decoded])
    return decoded


def _spec(raw: object) -> tuple[dict, list[tuple[dict, bytes]]]:
    if not isinstance(raw, dict):
        raise MaintError(400, "input", "each item must be an object")
    origin = raw.get("origin")
    if not isinstance(origin, dict) or not isinstance(origin.get("kind"), str) or not origin["kind"]:
        raise MaintError(400, "input", "origin.kind must name the collector")
    priority = raw.get("priority", DEFAULT_PRIORITY)
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 1000:
        raise MaintError(400, "input", "priority must be an integer from 0 to 1000")
    files = _decode_files(raw.get("files"))
    spec = {"origin": origin, "topic_key": _text(raw.get("topic_key"), "topic_key", 512),
            "priority": priority, "brief": _text(raw.get("brief"), "brief", 4000, required=False)}
    spec["item_key"] = item_key(origin["kind"], spec["topic_key"], [meta["sha256"] for meta, _ in files])
    return spec, files


# --- leases --------------------------------------------------------------------------

def _active(lease: dict | None, now: datetime) -> bool:
    """Live until min(expires_at, renewed_at + TTL), so no lease file outlasts one TTL."""
    if not lease:
        return False
    expires, renewed = _parse(lease.get("expires_at")), _parse(lease.get("renewed_at"))
    return bool(expires and renewed and renewed <= now + CLOCK_SKEW and now < min(expires, renewed + LEASE_TTL))


def _holder(lease: dict) -> dict:
    return {"holder": lease.get("holder"), "run": lease.get("run"), "expires_at": lease.get("expires_at")}


def _renew(bundle: Path, role: str, lease: dict, now: datetime) -> dict:
    lease.update(role=role, renewed_at=_iso(now), expires_at=_iso(now + LEASE_TTL))
    _write_json(_lease_path(bundle, role), lease)
    return lease


def _hold(bundle: Path, role: str, principal: str, run: str | None, now: datetime) -> dict:
    lease = _read_json(_lease_path(bundle, role))
    if not run or not _active(lease, now) or (lease.get("holder"), lease.get("run")) != (principal, run):
        extra = _holder(lease) if _active(lease, now) else {}
        raise MaintError(409, "lease_required", f"run {run!r} does not hold the {role} lease", **extra)
    return _renew(bundle, role, lease, now)


def _touch(bundle: Path, principal: str, run: str | None, now: datetime) -> None:
    """Any write call carrying X-AIWiki-Run renews the live leases that run holds."""
    for role in ROLES if run else ():
        lease = _read_json(_lease_path(bundle, role))
        if _active(lease, now) and (lease.get("holder"), lease.get("run")) == (principal, run):
            _renew(bundle, role, lease, now)


def renew(bundle: Path, *, principal: str, run: str | None) -> None:
    """Renew the live leases (principal, run) holds; every write call with X-AIWiki-Run does this."""
    with _LOCK:
        _touch(bundle, principal, run, _now())


def require_lease(bundle: Path, role: str, *, principal: str, run: str | None) -> dict:
    """Renew and return the live ``role`` lease held by (principal, run); else 409 lease_required."""
    with _LOCK:
        return _hold(bundle, role, principal, run, _now())


def acquire_lease(bundle: Path, role: str, *, principal: str, run: str | None) -> dict:
    """Take the run-level lease for ``role``. A live lease of another run is a 409 with its holder.

    Taking the maintainer lease starts a run: items left in progress by an earlier or
    interrupted run return to ready (class ``interrupted``) and parked items become ready.
    The run that already holds the live lease only renews it; its own item stays claimed.
    """
    if role not in ROLES:
        raise MaintError(404, "not_found", f"unknown lease role: {role}")
    run = _check_run(run)
    with _LOCK:
        now = _now()
        lease = _read_json(_lease_path(bundle, role))
        live = _active(lease, now)
        if live and (lease.get("holder"), lease.get("run")) != (principal, run):
            raise MaintError(409, "lease_held", f"the {role} lease is held by run {lease.get('run')}",
                             **_holder(lease))
        lease = {"holder": principal, "run": run,
                 "acquired_at": lease.get("acquired_at", _iso(now)) if live else _iso(now)}
        lease = _renew(bundle, role, lease, now)
        if role != "maintainer":
            return {"lease": lease}
        swept = {"interrupted": [], "unparked": [], "build_retry": []} if live else _begin_run(bundle, now)
        return {"lease": lease, **swept}


def release_lease(bundle: Path, role: str, *, principal: str, run: str | None) -> dict:
    """Release the run's lease (idempotent); its in-progress items return to ready."""
    if role not in ROLES:
        raise MaintError(404, "not_found", f"unknown lease role: {role}")
    with _LOCK:
        now = _now()
        path = _lease_path(bundle, role)
        lease = _read_json(path)
        if not lease or (lease.get("holder"), lease.get("run")) != (principal, run):
            if _active(lease, now):
                raise MaintError(409, "lease_held", f"the {role} lease is held by run {lease.get('run')}",
                                 **_holder(lease))
            return {"released": False, "interrupted": []}
        path.unlink(missing_ok=True)
        interrupted = []
        for item in _items(bundle) if role == "maintainer" else ():
            if item["status"] == "in_progress" and item.get("current_run") == run:
                _fail_attempt(item, run=run, cls="interrupted", detail="run released its lease", now=now)
                _save(bundle, item, now)
                interrupted.append(item["id"])
        return {"released": True, "interrupted": interrupted}


def _begin_run(bundle: Path, now: datetime) -> dict:
    interrupted, unparked, build_retry = [], [], []
    current = build()
    for item in _items(bundle):
        history = item["attempts"]["history"]
        if item["status"] == "in_progress":
            _fail_attempt(item, run=item.get("current_run"), cls="interrupted",
                          detail="run ended without resolving the item", now=now)
            interrupted.append(item["id"])
        elif item["status"] == "parked":
            item["status"] = "ready"
            unparked.append(item["id"])
        elif (item["status"] == "needs_human" and (item.get("resolution") or {}).get("reason") == "attempt_cap"
              and current and history and history[-1].get("build") != current
              and item.get("build_retry") != current):
            # A newer gate build may carry the fix: one extra attempt per build (the P0-A rule).
            item.update(status="ready", resolution=None, build_retry=current)
            build_retry.append(item["id"])
        else:
            continue
        _save(bundle, item, now)
    return {"interrupted": interrupted, "unparked": unparked, "build_retry": build_retry}


# --- items ---------------------------------------------------------------------------

def _fail_attempt(item: dict, *, run: str | None, cls: str, detail: str, now: datetime) -> None:
    """Record one failed attempt. A non-retryable class (P0: auth, disk, input) or a cap sends
    the item to needs_human, which blocks nothing else."""
    attempts = item["attempts"]
    attempts["history"].append({"run": run, "class": cls, "detail": redact(detail)[:500], "at": _iso(now),
                                "build": build()})
    if cls in COUNTED:
        attempts["counted"] += 1
    item["current_run"] = None
    capped = attempts["counted"] >= MAX_COUNTED or attempts["started"] >= MAX_STARTED
    if cls in NOT_RETRYABLE or capped:
        reason = "not_retryable" if cls in NOT_RETRYABLE else "attempt_cap"
        item.update(status="needs_human", resolution={"outcome": "needs_human", "reason": reason,
                                                      "class": cls, "at": _iso(now)})
    else:
        item["status"] = "ready" if cls == "interrupted" else "parked"


def _close(item: dict, outcome: str, reason: str | None, *, by: str, run: str | None, now: datetime,
           **extra) -> None:
    resolution = {"outcome": outcome, "reason": reason, "by": by, "run": run, "at": _iso(now), **extra}
    item.update(status=outcome, current_run=None, resolution=resolution)


def _intact(path: Path, sha: str) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if hashlib.sha256(data).hexdigest() == sha else None


def _write_blobs(bundle: Path, item_id: str, files: list[tuple[dict, bytes]]) -> None:
    """Write each blob unless an intact copy is already there; a damaged one is replaced."""
    for meta, data in files:
        blob = _item_dir(bundle, item_id) / "files" / meta["sha256"]
        if _intact(blob, meta["sha256"]) is None:
            _atomic_write(blob, data)


def _blob(bundle: Path, item_id: str, meta: dict) -> bytes:
    """Frozen evidence, re-hashed on every read: bytes that no longer match fail closed."""
    data = _intact(_item_dir(bundle, item_id) / "files" / meta["sha256"], meta["sha256"])
    if data is None:
        raise MaintError(500, "evidence_corrupt", f"{item_id}: evidence {meta['name']} is missing or does not "
                                                  "match its sha256; re-collect it")
    return data


def _enqueue(bundle: Path, spec: dict, files: list[tuple[dict, bytes]], principal: str, items: list[dict],
             now: datetime) -> dict:
    key = spec["item_key"]
    for item in items:
        if key == item["item_key"] or any(v.get("item_key") == key for v in item["versions"]):
            _write_blobs(bundle, item["id"], files)  # the genuine bytes repair a damaged blob
            return {"id": item["id"], "item_key": key, "result": "duplicate", "status": item["status"]}
    for target in (i for i in items if i["status"] in MERGEABLE and i["topic_key"] == spec["topic_key"]):
        # Same topic still waiting: fold the newer collection in. Same-named files take the
        # newer bytes, new files are appended, and the replaced entries stay in versions. A
        # merge that would overflow the item's caps queues a new item instead.
        by_name = {f["name"]: f for f in target["files"]}
        replaced = [by_name[m["name"]] for m, _ in files
                    if m["name"] in by_name and by_name[m["name"]]["sha256"] != m["sha256"]]
        by_name.update((m["name"], m) for m, _ in files)
        if not _fits(list(by_name.values())):
            continue
        _write_blobs(bundle, target["id"], files)
        target["versions"].append({"item_key": key, "merged_at": _iso(now), "origin": target["origin"],
                                   "brief": target["brief"], "replaced": replaced})
        target.update(files=list(by_name.values()), origin=spec["origin"], brief=spec["brief"],
                      priority=max(target["priority"], spec["priority"]), collected_by=principal,
                      collected_at=_iso(now))
        _save(bundle, target, now)
        return {"id": target["id"], "item_key": key, "result": "merged", "status": target["status"]}
    item_id = "it_" + key[:12]
    if (_item_dir(bundle, item_id) / "item.json").exists():
        raise MaintError(500, "internal", f"item id collision: {item_id}")
    item = {"id": item_id, "status": "ready", **{k: spec[k] for k in ("origin", "topic_key", "item_key")},
            "priority": spec["priority"], "brief": spec["brief"], "files": [meta for meta, _ in files],
            "collected_by": principal, "collected_at": _iso(now),
            "attempts": {"started": 0, "counted": 0, "history": []},
            "current_run": None, "resolution": None, "versions": [], "created_at": _iso(now)}
    # Blobs first, item.json last: the item exists only once all of its evidence is durable.
    _write_blobs(bundle, item_id, files)
    _save(bundle, item, now)
    items.append(item)
    return {"id": item_id, "item_key": key, "result": "created", "status": "ready"}


def enqueue(bundle: Path, items: object, *, principal: str, run: str | None = None) -> dict:
    """Freeze collected evidence as ready items: idempotent by item_key, merged by topic_key."""
    if not isinstance(items, list) or not items:
        raise MaintError(400, "input", "items must be a nonempty list")
    specs = [_spec(raw) for raw in items]  # reject the whole batch before writing anything; past
    # this point only an I/O failure can stop a batch, and a retry is idempotent by item_key
    with _LOCK:
        now = _now()
        _touch(bundle, principal, run, now)
        current = _items(bundle)
        results = [_enqueue(bundle, spec, files, principal, current, now) for spec, files in specs]
    return {"items": results, **{r: sum(x["result"] == r for x in results)
                                  for r in ("created", "merged", "duplicate")}}


def _effective_priority(item: dict, now: datetime) -> int:
    """Aging: every whole day an item has waited adds AGING_PER_DAY, so nothing starves."""
    created = _parse(item.get("created_at")) or now
    return item["priority"] + AGING_PER_DAY * max(0, (now - created).days)


def next_item(bundle: Path, *, principal: str, run: str | None) -> dict:
    """Claim the highest-priority ready item for the lease-holding run.

    A run gets back the item it already has in progress, so a lost response never
    orphans an item or spends another attempt.
    """
    with _LOCK:
        now = _now()
        _hold(bundle, "maintainer", principal, run, now)
        items = _items(bundle)
        ready = [i for i in items if i["status"] == "ready"]
        current = next((i for i in items if i["status"] == "in_progress" and i.get("current_run") == run), None)
        if current is not None:
            return {"item": current, "resumed": True, "ready": len(ready)}
        if not ready:
            return {"item": None, "resumed": False, "ready": 0}
        ready.sort(key=lambda i: (-_effective_priority(i, now), i["created_at"], i["id"]))
        item = ready[0]
        item.update(status="in_progress", current_run=run)
        item["attempts"]["started"] += 1
        _save(bundle, item, now)
        return {"item": item, "resumed": False, "ready": len(ready) - 1}


def _in_progress(bundle: Path, item_id: str, run: str | None) -> dict:
    item = _load(bundle, item_id)
    if item["status"] != "in_progress" or item.get("current_run") != run:
        raise MaintError(409, "item_not_in_progress",
                         f"{item_id} is {item['status']}, not in progress for run {run!r}",
                         status=item["status"], current_run=item.get("current_run"))
    return item


def add_file(bundle: Path, item_id: str, raw: object, *, principal: str, run: str | None) -> dict:
    """Freeze one more evidence file into the run's in-progress item (``maint add-evidence``)."""
    [(meta, data)] = _decode_files([raw])
    with _LOCK:
        now = _now()
        _hold(bundle, "maintainer", principal, run, now)
        item = _in_progress(bundle, item_id, run)
        existing = next((f for f in item["files"] if f["name"] == meta["name"]), None)
        if existing is not None:
            if existing["sha256"] == meta["sha256"]:
                return item
            raise MaintError(409, "file_exists", f"{item_id} already holds different evidence named {meta['name']}")
        _check_size([*item["files"], meta])
        _write_blobs(bundle, item_id, [(meta, data)])
        item["files"].append({**meta, "added_by": principal, "added_at": _iso(now)})
        _save(bundle, item, now)
        return item


def _split(bundle: Path, parent: dict, children: object, principal: str, now: datetime) -> list[str]:
    """Queue children that partition the parent's files under distinct topics; idempotent by
    item_key. Every file lands in exactly one child, so a split can drop no evidence, and no
    two children can merge back into one item with fresh attempt counters."""
    if not isinstance(children, list) or not 2 <= len(children) <= MAX_CHILDREN:
        raise MaintError(400, "input", f"split needs 2 to {MAX_CHILDREN} children")
    by_name = {f["name"]: f for f in parent["files"]}
    seen: set[str] = set()
    topics: set[str] = set()
    specs = []
    for index, child in enumerate(children, 1):
        names = child.get("files") if isinstance(child, dict) else None
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise MaintError(400, "input", f"child {index}: files must be a nonempty list of names")
        unknown = sorted(set(names) - set(by_name))
        if unknown or seen & set(names) or len(set(names)) != len(names):
            raise MaintError(400, "input", f"child {index}: unknown or repeated files: {unknown or names}")
        seen.update(names)
        topic = _text(child.get("topic_key", f"{parent['topic_key']}#split-{index}"), "topic_key", 512)
        if topic in topics:
            raise MaintError(400, "input", f"child {index}: topic_key {topic!r} repeats another child's")
        topics.add(topic)
        spec = {"origin": parent["origin"], "topic_key": topic, "priority": parent["priority"],
                "brief": _text(child.get("brief", f"split {index}/{len(children)} of {parent['id']}"),
                               "brief", 4000, required=False),
                "item_key": item_key(parent["origin"]["kind"], topic, [by_name[n]["sha256"] for n in names])}
        specs.append((spec, [by_name[n] for n in names]))
    if missing := sorted(set(by_name) - seen):
        raise MaintError(400, "input", f"split must place every file in a child; missing: {missing}")
    loaded = [(spec, [(meta, _blob(bundle, parent["id"], meta)) for meta in files]) for spec, files in specs]
    current = _items(bundle)
    return [_enqueue(bundle, spec, files, principal, current, now)["id"] for spec, files in loaded]


def _check_reason(outcome: str, reason: object) -> str:
    reason = _text(reason, "reason", 500)
    if outcome == "skipped" and reason not in SKIP_REASONS and not (
            reason.startswith("duplicate_of:") and reason[len("duplicate_of:"):].strip()):
        raise MaintError(400, "input", "skip reason must be one of " + ", ".join(sorted(SKIP_REASONS))
                         + " or duplicate_of:<path>")
    return reason


def resolve(bundle: Path, item_id: str, body: object, *, principal: str, run: str | None) -> dict:
    """Close or park the run's in-progress item: ``{outcome, reason, class?, children?, note?}``."""
    body = body if isinstance(body, dict) else {}
    outcome = body.get("outcome")
    if not isinstance(outcome, str) or outcome not in AGENT_OUTCOMES:
        raise MaintError(400, "input", "outcome must be one of " + ", ".join(sorted(AGENT_OUTCOMES)))
    cls = body.get("class")
    if outcome == "parked" and (not isinstance(cls, str) or cls not in PARK_CLASSES):
        raise MaintError(400, "input", "park class must be one of " + ", ".join(sorted(PARK_CLASSES)))
    note = _text(body.get("note"), "note", 2000, required=False) or None
    with _LOCK:
        now = _now()
        _hold(bundle, "maintainer", principal, run, now)
        item = _in_progress(bundle, item_id, run)
        if outcome == "parked":
            _fail_attempt(item, run=run, cls=cls,
                          detail=_text(body.get("reason"), "reason", 2000, required=False), now=now)
        elif outcome == "split":
            children = _split(bundle, item, body.get("children"), principal, now)
            _close(item, "split", body.get("reason") if isinstance(body.get("reason"), str) else None,
                   by=principal, run=run, now=now, children=children, note=note)
        else:
            _close(item, outcome, _check_reason(outcome, body.get("reason")), by=principal, run=run, now=now,
                   note=note)
        _save(bundle, item, now)
        return item


def close_curated(bundle: Path, item_ids: list[str], *, job_id: str, commit: str | None, principal: str,
                  run: str | None) -> list[str]:
    """Mark items curated by a committed changeset job and return the items this job closed.
    Runs after the commit, so it never raises for an item that is missing or already terminal;
    it leaves that item alone. An item this job closed before a restart still counts as closed."""
    closed = []
    with _LOCK:
        now = _now()
        for item_id in item_ids:
            item = _read_item(bundle, item_id) if isinstance(item_id, str) else None
            if item is None or item["status"] in TERMINAL:
                if item and item["status"] == "curated" and (item.get("resolution") or {}).get("job") == job_id:
                    closed.append(item_id)
                continue
            _close(item, "curated", None, by=principal, run=run, now=now, job=job_id, commit=commit)
            _save(bundle, item, now)
            closed.append(item_id)
    return closed


def get_item(bundle: Path, item_id: str) -> dict:
    return _load(bundle, item_id)


def evidence(bundle: Path, item_id: str, name: str, sha256: str | None = None) -> tuple[dict, bytes]:
    """One frozen evidence file of an item: its metadata and re-hashed bytes. With ``sha256``,
    the exact bytes a changeset named, even after a merge replaced the file under that name."""
    item = _load(bundle, item_id)
    replaced = [old for version in item["versions"] for old in version.get("replaced") or []
                if isinstance(old, dict) and isinstance(old.get("sha256"), str) and _SHA.fullmatch(old["sha256"])]
    entry = next((f for f in item["files"] + replaced
                  if f.get("name") == name and sha256 in (None, f["sha256"])), None)
    if entry is None:
        raise MaintError(404, "not_found", f"{item_id} has no file named {name}")
    return entry, _blob(bundle, item_id, entry)


def read_file(bundle: Path, item_id: str, name: str) -> bytes:
    return evidence(bundle, item_id, name)[1]


def active_lease(bundle: Path, role: str) -> dict | None:
    """The live ``role`` lease, if any. The writer defers Codex audits while a maintainer run holds one."""
    lease = _read_json(_lease_path(bundle, role))
    return lease if _active(lease, _now()) else None


def list_items(bundle: Path, *, status: str | None = None, origin: str | None = None, limit: int = 50) -> dict:
    rows = [item for item in _items(bundle) if (status is None or item["status"] == status)
            and (origin is None or item["origin"].get("kind") == origin)]
    rows.sort(key=lambda item: (item["created_at"], item["id"]))
    return {"items": rows[:limit], "shown": min(limit, len(rows)), "total": len(rows),
            "truncated": len(rows) > limit}


# --- admin ---------------------------------------------------------------------------

def admin_retry(bundle: Path, item_id: str, *, principal: str, reason: str | None = None) -> dict:
    """Reopen a needs_human (or skipped/parked/…) item as ready with fresh attempt counters.

    A curated item reopens only once an admin revert undid the changeset that curated it:
    its evidence is still good, and nothing of it is left in the bundle.
    """
    with _LOCK:
        now = _now()
        item = _load(bundle, item_id)
        reverted = item["status"] == "curated" and (item.get("resolution") or {}).get("job") in I.reverted_by(bundle)
        if item["status"] not in REOPENABLE and not reverted:
            raise MaintError(409, "not_reopenable", f"{item_id} is {item['status']}", status=item["status"])
        item.setdefault("reopened", []).append({"by": principal, "at": _iso(now), "from": item["status"],
                                                "resolution": item.get("resolution"), "reason": reason})
        item["attempts"].update(started=0, counted=0)
        item.update(status="ready", resolution=None, current_run=None)
        _save(bundle, item, now)
        return item


def admin_resolve(bundle: Path, item_id: str, body: object, *, principal: str) -> dict:
    """Close any unfinished item by hand, without a lease: ``{outcome, reason, note?}``."""
    body = body if isinstance(body, dict) else {}
    outcome = body.get("outcome")
    if not isinstance(outcome, str) or outcome not in ADMIN_OUTCOMES:
        raise MaintError(400, "input", "outcome must be one of " + ", ".join(sorted(ADMIN_OUTCOMES)))
    reason = _check_reason(outcome, body.get("reason"))
    note = _text(body.get("note"), "note", 2000, required=False) or None
    with _LOCK:
        now = _now()
        item = _load(bundle, item_id)
        if item["status"] in TERMINAL - {"needs_human"}:
            raise MaintError(409, "item_closed", f"{item_id} is already {item['status']}", status=item["status"])
        _close(item, outcome, reason, by=principal, run=None, now=now, note=note, admin=True)
        _save(bundle, item, now)
        return item


# --- cursors -------------------------------------------------------------------------

def _check_cursor(name: str) -> None:
    if name not in CURSORS:
        raise MaintError(404, "not_found", f"unknown cursor: {name} (expected one of {', '.join(CURSORS)})")


def get_cursor(bundle: Path, name: str) -> dict:
    _check_cursor(name)
    cursor = _read_json(_cursor_path(bundle, name))
    if cursor is None:
        raise MaintError(404, "not_found", f"cursor {name} has never been written")
    return cursor


def _unquote(etag: str) -> str:
    etag = etag.strip()
    return (etag[2:] if etag.startswith("W/") else etag).strip('"')


def put_cursor(bundle: Path, name: str, value: object, *, if_match: str | None, if_none_match: str | None,
               principal: str, run: str | None = None) -> dict:
    """Compare-and-swap: ``If-Match: <etag>`` replaces, ``If-None-Match: *`` creates; else 428."""
    _check_cursor(name)
    if not isinstance(value, dict):
        raise MaintError(400, "input", "cursor value must be an object")
    if if_match is None and (if_none_match or "").strip() != "*":
        raise MaintError(428, "precondition_required", "send If-Match: <etag> (or If-None-Match: * to create)")
    run = None if run is None else _check_run(run)
    with _LOCK:
        path = _cursor_path(bundle, name)
        current = _read_json(path)
        if if_match is None:
            conflict = current is not None
        else:
            conflict = current is None or _unquote(if_match) != current.get("etag")
        if conflict:
            raise MaintError(412, "cursor_conflict", f"cursor {name} changed; re-read it and retry", current=current)
        now = _now()
        _touch(bundle, principal, run, now)
        record = {"name": name, "value": value, "updated_at": _iso(now), "updated_by": principal, "run": run}
        chain = {**record, "previous": current and current.get("etag")}
        record["etag"] = hashlib.sha256(json.dumps(chain, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        _write_json(path, record)
        return record


# --- status --------------------------------------------------------------------------

def _last_changeset(jobs: list[dict]) -> dict | None:
    changesets = [job for job in jobs if job.get("mode") == "changeset"]
    latest = max(changesets, key=lambda job: (str(job.get("created") or ""), str(job.get("id"))), default=None)
    return latest and {k: latest.get(k) for k in ("id", "status", "created", "finished", "commit")}


def status(bundle: Path) -> dict:
    """Deterministic SLO snapshot for ``maint status`` and the gate watchdog."""
    now = _now()
    with _LOCK:
        items, corrupt = _scan(bundle)
        leases = {role: _read_json(_lease_path(bundle, role)) for role in ROLES}
        cursors = {name: _read_json(_cursor_path(bundle, name)) for name in CURSORS}
    by_status: dict[str, dict] = {}
    ready_by_origin: dict[str, dict] = {}
    for item in items:
        groups = [by_status.setdefault(item["status"], {"count": 0, "oldest_created_at": None})]
        if item["status"] == "ready":
            groups.append(ready_by_origin.setdefault(item["origin"].get("kind"),
                                                     {"count": 0, "oldest_created_at": None}))
        for group in groups:
            group["count"] += 1
            if group["oldest_created_at"] is None or item["created_at"] < group["oldest_created_at"]:
                group["oldest_created_at"] = item["created_at"]
    for group in [*by_status.values(), *ready_by_origin.values()]:
        group["oldest_age_s"] = _age_s(group["oldest_created_at"], now)
    pending = I.pending_audits(bundle, older_than_hours=0, limit=1)
    mode = os.environ.get("AIWIKI_AUDIT", "").strip() or "codex"  # as app.MODES reads it for /whoami
    jobs = [job for path in (bundle / ".okf" / "jobs").glob("*.json") if (job := _read_json(path)) is not None]
    # A queued (or lease-deferred) audit is not pending, but it is still backlog.
    queued_audits = [str(job.get("created") or "") for job in jobs
                     if job.get("kind") == "audit" and job.get("status") == "queued"]
    return {
        "now": _iso(now),
        "cursors": {name: cursor and {"updated_at": cursor.get("updated_at"), "run": cursor.get("run"),
                                      "age_s": _age_s(cursor.get("updated_at"), now)}
                    for name, cursor in cursors.items()},
        "items": by_status,
        "ready_by_origin": ready_by_origin,
        "needs_human": [{"id": i["id"], "topic_key": i["topic_key"], "since": i.get("updated_at"),
                         "resolution": i.get("resolution")} for i in items if i["status"] == "needs_human"],
        "corrupt_items": corrupt,
        "leases": {role: lease and {**_holder(lease), "active": _active(lease, now)}
                   for role, lease in leases.items()},
        "audit": {"mode": mode, "pending": pending["total"],
                  "oldest_finished": pending["jobs"][0].get("finished") if pending["jobs"] else None,
                  "queued": len(queued_audits), "oldest_queued": min(queued_audits, default=None)},
        # The §7 deploy guard refuses while a lease is active or a job is running.
        "jobs": {state: sum(job.get("status") == state for job in jobs) for state in ("queued", "running")},
        # Phase 1-3: Claude curates, the writer's Codex audits on another host (design §5.1).
        "auditor_independence": "strong" if mode == "codex" else None,
        "last_changeset": _last_changeset(jobs),
        "service": service_identity(),
    }
