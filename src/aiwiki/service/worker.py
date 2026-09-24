"""Serial curation worker.

Ingest only *queues* work; a single background thread drains the queue one job at a
time. Serializing curation is what makes many concurrent writers safe: two curation
passes never touch the bundle or its git tree at once, so the only contention left is
between this worker and *other* writers' pushes — which curate.py handles by rebasing.

The queue is ordered by kind (design §2.6): changesets and admin reverts, then Codex
audits, then Codex ingests, first in first out within a kind; a running job is never
preempted. Until a bundle commits changesets its Codex audits and ingests stay in one FIFO,
as before. Once it does, a Codex audit of it waits while its maintainer run holds the lease,
so it never takes the lock for minutes in the middle of a run; a lease on any other bundle
holds nothing back.

On startup, queued jobs left by a previous run are re-enqueued (a changeset from its
request in ``.okf/changesets``, a revert from its own job). Interrupted running jobs are
reconciled from durable Git transaction metadata before being marked failed.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..runtime import audit, changeset, curate, revert
from ..runtime.failure import classify, failure
from . import ingest as I
from . import maint_state as M

PRIORITY = {"changeset": 0, "revert": 0, "audit": 1, "ingest": 2}  # lower runs first
# Installed by the app (design §2.11): the bundles whose changesets commit (none while
# AIWIKI_DISABLE=changesets), and whether a principal may still propose one to a bundle.
# A queued changeset is checked again when it runs, so a rollback or a revoked principal
# (§8.5) stops it too. AUDIT_BUNDLES are the committing bundles whose changesets queue their
# own Codex audit (all but AIWIKI_CODEX_AUDIT_MANUAL).
COMMIT_BUNDLES: frozenset[str] = frozenset()
AUDIT_BUNDLES: frozenset[str] = frozenset()


def actor_of(principal: str, bundle: str) -> str | None:
    """The actor a principal now in force stamps on a changeset to ``bundle``, or None when it
    may no longer propose one there."""
    return None  # until the app installs its principals


_q: queue.PriorityQueue = queue.PriorityQueue()
_order = itertools.count()  # FIFO within a priority
_deferred: list[tuple] = []  # Codex audits waiting out a maintainer run (worker thread only)
_finished: dict[Path, threading.Event] = {}  # changeset or revert job -> set once its receipt is final
# Changeset job -> (principal, changeset_sha256) as admitted. The job and its request wait in
# .okf, which an in-place Codex audit can write, so a run holds them to this memory.
_admitted: dict[Path, tuple[str, str]] = {}
DEFER_POLL_S = 30
_started = False
_lock = threading.Lock()
_mutation_lock = threading.Lock()
_lifecycle_lock = threading.Lock()
_read_condition = threading.Condition()
_mutation_active = False
_mutation_pending = False
_active_readers = 0
SWEEP_INTERVAL_S = 60
REMOTE_CONTAINS = "contains"
REMOTE_ABSENT = "absent"
REMOTE_UNKNOWN = "unknown"
RECOVERY_PENDING = "pending_remote_confirmation"


class MutationBusy(RuntimeError):
    """Another serialized writer mutation is currently in progress."""


class ReadBusy(RuntimeError):
    """A knowledge-tree mutation is active or about to start."""


@contextmanager
def serialized_mutation(*, blocking: bool = True):
    """Serialize writes and wait for in-flight semantic reads to finish."""
    acquired = _mutation_lock.acquire(blocking=blocking)
    if not acquired:
        raise MutationBusy("another bundle mutation is in progress")
    global _mutation_active, _mutation_pending
    try:
        with _read_condition:
            _mutation_pending = True
            while _active_readers:
                _read_condition.wait()
            _mutation_pending = False
            _mutation_active = True
        yield
    finally:
        with _read_condition:
            _mutation_active = False
            _mutation_pending = False
            _read_condition.notify_all()
        _mutation_lock.release()


def is_mutating() -> bool:
    with _read_condition:
        return _mutation_active or _mutation_pending


@contextmanager
def serialized_read():
    """Hold a semantic read window or fail fast while a writer owns the live tree."""
    global _active_readers
    with _read_condition:
        if _mutation_active or _mutation_pending:
            raise ReadBusy("bundle mutation in progress")
        _active_readers += 1
    try:
        yield
    finally:
        with _read_condition:
            _active_readers -= 1
            if not _active_readers:
                _read_condition.notify_all()


@contextmanager
def serialized_lifecycle():
    """Serialize short create/delete/receive/sweep decisions without blocking on LLM work."""
    with _lifecycle_lock:
        yield


def _save_job(path: Path, job: dict) -> None:
    curate._save(path, job)


def _remote_contains(root: Path, branch: str, commit: str) -> str:
    """Determine whether origin/<branch> contains the service-created commit.

    A transport/authentication failure is not evidence that a previously attempted
    push failed. Callers must preserve the transaction until containment can be
    checked again.
    """
    if not curate._has_remote(root):
        return REMOTE_ABSENT
    if curate._git(root, "fetch", "--quiet").returncode != 0:
        return REMOTE_UNKNOWN
    result = curate._git(root, "merge-base", "--is-ancestor", commit, f"origin/{branch}")
    if result.returncode == 0:
        return REMOTE_CONTAINS
    if result.returncode == 1:
        return REMOTE_ABSENT
    return REMOTE_UNKNOWN


def _restore_ingest_source(bundle: Path, job: dict) -> None:
    """Restore a moved, Git-ignored inbox source from its durable SHA snapshot."""
    source = job.get("source")
    recovery = job.get("recovery_source")
    expected = job.get("sha256")
    if not all(isinstance(value, str) and value for value in (source, recovery, expected)):
        return
    destination = (bundle / source).resolve()
    backup = (bundle / recovery).resolve()
    try:
        destination.relative_to(bundle.resolve())
        backup.relative_to((bundle / ".okf" / "recovery").resolve())
    except ValueError:
        return
    if not backup.is_file() or backup.is_symlink():
        return
    data = backup.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        return
    if destination.is_file() and not destination.is_symlink():
        if hashlib.sha256(destination.read_bytes()).hexdigest() == expected:
            return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def _reconcile_running(bundle: Path, job: dict) -> str:
    """Recover one interrupted Git job and return the terminal reconciliation outcome."""
    root = curate._repo_root(bundle)
    base = job.get("base_revision")
    branch = job.get("base_branch")
    commit = job.get("commit")
    phase = job.get("phase")
    pushed = bool((job.get("git") or {}).get("pushed")) if isinstance(job.get("git"), dict) else False

    if root is None or not isinstance(base, str) or not base:
        return "recovery metadata unavailable; tree was not changed by recovery"
    if root.resolve() != bundle.resolve():
        return "bundle does not own its Git repository; tree was not changed by recovery"
    if not isinstance(branch, str) or not branch or curate._branch(root) != branch:
        return "recovery branch does not match the interrupted transaction; tree was not changed"

    # Read ignored recovery bytes before Git cleanup; the recovery directory itself
    # may be untracked and is intentionally removed with the half-written tree.
    recovery_bytes: bytes | None = None
    recovery_rel = job.get("recovery_source")
    expected = job.get("sha256")
    if isinstance(recovery_rel, str) and isinstance(expected, str):
        recovery_path = (bundle / recovery_rel).resolve()
        try:
            recovery_path.relative_to((bundle / ".okf" / "recovery").resolve())
        except ValueError:
            pass
        else:
            if recovery_path.is_file() and not recovery_path.is_symlink():
                candidate = recovery_path.read_bytes()
                if hashlib.sha256(candidate).hexdigest() == expected:
                    recovery_bytes = candidate

    # Always leave an interrupted rebase before inspecting or resetting the tree.
    curate._git(root, "rebase", "--abort")
    has_remote = curate._has_remote(root)
    if isinstance(commit, str) and commit:
        if has_remote:
            remote_state = _remote_contains(root, branch, commit)
        else:
            local = curate._git(root, "merge-base", "--is-ancestor", commit, "HEAD")
            remote_state = (
                REMOTE_CONTAINS if local.returncode == 0
                else REMOTE_ABSENT if local.returncode == 1
                else REMOTE_UNKNOWN
            )
    else:
        remote_state = REMOTE_ABSENT
    if remote_state == REMOTE_UNKNOWN:
        job["recovery_pending"] = (
            "remote commit containment could not be confirmed; retry recovery"
        )
        return RECOVERY_PENDING
    job.pop("recovery_pending", None)
    if remote_state == REMOTE_CONTAINS and phase in {"committed", "pushed"}:
        # Push may have succeeded immediately before the process died. Never erase a
        # transaction already observable on the remote. The remote may since have
        # advanced past this job commit, so reconcile to its current branch head rather
        # than moving the local clone backwards to the job's historical commit.
        target = f"origin/{branch}" if has_remote else "HEAD"
        if curate._git(root, "reset", "--hard", target).returncode != 0:
            return "durable commit exists, but local reconciliation failed"
        curate._discard_working_tree(root)
        if job.get("kind", "ingest") == "audit":
            report = job.get("audit")
            if not isinstance(report, dict) or report.get("status") not in {"passed", "needs_attention"}:
                return "remote contains audit commit, but durable audit result is incomplete"
        else:
            validation = job.get("validation")
            if not isinstance(validation, dict) or validation.get("status") != "passed":
                return "remote contains ingest commit, but durable validation result is incomplete"
            if not isinstance(job.get("concept_files"), list):
                return "remote contains ingest commit, but durable concept scope is incomplete"
        job["status"] = "done"
        job["phase"] = "done"
        git = job.get("git") if isinstance(job.get("git"), dict) else {}
        job["git"] = {
            **git,
            "committed": True,
            "pushed": has_remote,
            "commit": commit,
        }
        job["recovered"] = "remote_contains_commit" if has_remote else "local_contains_commit"
        return "done"

    # A merely local commit (or any pre-commit mutation) is not published truth.
    # Return to the exact clean base and restore the ignored inbox submission.
    if curate._git(root, "reset", "--hard", base).returncode != 0:
        return "failed to reset interrupted transaction to base revision"
    curate._discard_working_tree(root)
    if job.get("kind", "ingest") == "ingest":
        if recovery_bytes is not None:
            source = job.get("source")
            if isinstance(source, str):
                destination = (bundle / source).resolve()
                try:
                    destination.relative_to(bundle.resolve())
                except ValueError:
                    pass
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(recovery_bytes)
        else:
            _restore_ingest_source(bundle, job)
    job["phase"] = "rolled_back"
    if phase == "pushed" or pushed:
        return "push was recorded but remote does not contain the job commit; rolled back"
    return "interrupted transaction rolled back to base revision"


def _classify_failed(job_path: Path) -> None:
    """Give every terminal failed job a structured ``failure`` record (early-return paths)."""
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(job, dict) and job.get("status") == "failed" and not isinstance(job.get("failure"), dict):
        job["failure"] = classify(job)
        _save_job(job_path, job)


def _put(kind: str, bundle: Path, subject: str, job_path: Path) -> None:
    # A bundle's Codex audits go ahead of Codex ingests only once its changesets commit and
    # queue their own audits; a manual (shadow) bundle's audits never pass production's work.
    priority = PRIORITY["ingest"] if kind == "audit" and bundle.name not in AUDIT_BUNDLES else PRIORITY[kind]
    _q.put((priority, next(_order), (kind, bundle, subject, job_path)))


def submit(bundle: Path, source_rel: str, job_path: Path) -> None:
    _put("ingest", bundle, source_rel, job_path)


def submit_audit(bundle: Path, parent_job: str, job_path: Path) -> None:
    _put("audit", bundle, parent_job, job_path)


def submit_changeset(bundle: Path, job_path: Path, *, principal: str | None = None, digest: str | None = None) -> None:
    """Queue a changeset job ahead of all Codex work; its request is ``I.changeset_path``.

    ``principal`` and ``digest`` (its changeset_sha256) are what intake admitted; recovery
    after a restart has only the job file to go by.
    """
    if principal is not None and digest is not None:
        _admitted[job_path] = (principal, digest)
    _finished.setdefault(job_path, threading.Event())
    _put("changeset", bundle, job_path.stem, job_path)


def submit_revert(bundle: Path, job_path: Path) -> None:
    """Queue an admin revert (design §8.5) with the changesets, ahead of all Codex work."""
    _finished.setdefault(job_path, threading.Event())
    _put("revert", bundle, job_path.stem, job_path)


def wait(job_path: Path, timeout: float) -> None:
    """Block until a queued changeset's or revert's receipt is final, or ``timeout`` seconds pass."""
    event = _finished.get(job_path)
    if event is not None:
        event.wait(timeout)


def _maintaining(bundle: Path) -> bool:
    try:
        return M.active_lease(bundle, "maintainer") is not None
    except OSError:
        return False


def _take() -> tuple:
    """The next job by priority; a Codex audit of a bundle that commits changesets waits
    while its maintainer run lasts (design §2.6). Elsewhere a lease defers nothing."""
    while True:
        for entry in [entry for entry in _deferred if not _maintaining(entry[2][1])]:
            _deferred.remove(entry)
            _q.put(entry)
            _q.task_done()  # the put counts it again, so join() never sees it finished
        try:
            entry = _q.get(timeout=DEFER_POLL_S if _deferred else None)
        except queue.Empty:
            continue
        kind, bundle = entry[2][:2]
        if kind == "audit" and bundle.name in COMMIT_BUNDLES and _maintaining(bundle):
            _deferred.append(entry)  # still unfinished until it runs
            continue
        return entry


def _run() -> None:
    while True:
        _priority, _n, (kind, bundle, subject, job_path) = _take()
        try:
            with serialized_mutation():
                try:
                    if kind == "audit" and subject in I.reverted_by(bundle):
                        _skip_reverted(job_path)
                    elif kind == "audit":
                        audit.run(bundle, subject, job_path)
                    elif kind == "changeset":
                        _run_changeset(bundle, job_path)
                    elif kind == "revert":
                        revert.run(bundle, job_path)
                    else:
                        curate.run(bundle, subject, job_path)
                finally:
                    _classify_failed(job_path)
        except Exception:  # noqa: BLE001 — runtimes record their own failures; never kill the worker
            pass
        finally:
            event = _finished.pop(job_path, None)
            if event is not None:
                event.set()
            _q.task_done()


def _skip_reverted(job_path: Path) -> None:
    """An admin reverted the changeset this Codex audit reviews: nothing of it is left to verify."""
    job = json.loads(job_path.read_text(encoding="utf-8"))
    detail = f"changeset {job.get('parent_job')} was reverted before its audit ran"
    job.update(status="failed", error=detail, finished=curate._now(),
               failure=failure("input", stage="intake", detail=detail, retryable=False))
    _save_job(job_path, job)


def closed_rejection(items: list[dict]) -> dict:
    """409 work_item_closed: an item once closed takes no further changeset (design §2.7)."""
    errors = []
    for item in items:
        resolution = item.get("resolution") or {}
        errors.append(changeset._error("work_item_closed", f"{item['id']} is already {item['status']}",
                                       item=item["id"], closed_by=resolution.get("job") or resolution.get("by")))
    return changeset._rejected({}, errors)


def _run_changeset(bundle: Path, job_path: Path) -> None:
    """Run one queued changeset job from its persisted request (design §2.5 G5–G15). The
    request is only needed while the job is queued, so it is deleted once the job has run."""
    admitted = _admitted.pop(job_path, None)
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if job.get("status") != "queued":
        return  # a duplicate queue entry of a job that already ran
    try:
        _run_queued(bundle, job_path, job, admitted)
    finally:
        I.changeset_path(bundle, job_path.stem).unlink(missing_ok=True)


def _run_queued(bundle: Path, job_path: Path, job: dict, admitted: tuple[str, str] | None) -> None:
    """Admit the job again as it runs (G0 and G3b may have changed while it queued), then run it.

    The actor is the one its principal stamps now, never a value read back from ``.okf``; a
    job or request that no longer matches what intake admitted fails without running.
    """
    principal, digest = admitted or (str(job.get("principal")), job.get("changeset_sha256"))
    actor = actor_of(principal, bundle.name)
    if bundle.name not in COMMIT_BUNDLES or actor is None:
        detail = f"{principal} may no longer commit changesets to '{bundle.name}'"
        job.update(status="rejected", http_status=403, errors=[changeset._error("forbidden", detail)],
                   failure=failure("auth", stage="intake", detail=detail), finished=curate._now())
        _save_job(job_path, job)
        return
    try:
        record = json.loads(I.changeset_path(bundle, job_path.stem).read_text(encoding="utf-8"))
        request = record["request"]
        evidence = [changeset.EvidenceFile(part["name"], M.evidence(bundle, *part["name"].split("/", 1),
                                                                    part["sha256"])[1], part.get("origin") or {})
                    for part in record["evidence_files"]]
        changed = (job.get("principal"), job.get("changeset_sha256")) != (principal, digest) or digest != \
            changeset.changeset_sha256(request, {item.name: hashlib.sha256(item.data).hexdigest() for item in evidence})
        items = [M.get_item(bundle, item_id) for item_id in job.get("work_items") or []]
        closed = [item for item in items if item["status"] in M.TERMINAL]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, M.MaintError) as exc:
        job.update(status="failed", error=f"changeset request or evidence unusable: {exc}", finished=curate._now(),
                   failure=failure("internal", stage="intake", detail=exc, retryable=False))
        _save_job(job_path, job)
        return
    if changed:
        detail = "the queued job or request no longer matches what was admitted; resubmit the changeset"
        job.update(status="failed", error=detail, finished=curate._now(),
                   failure=failure("internal", stage="intake", detail=detail, retryable=False))
        _save_job(job_path, job)
        return
    if closed:
        # G3b again: an item another changeset closed while this one waited in the queue.
        job.update(closed_rejection(closed), finished=curate._now())
        _save_job(job_path, job)
        return
    curate.run_changeset(bundle, job_path, request, actor=actor, evidence_files=evidence,
                         on_done=lambda done: _closeout(bundle, done))


def _closeout(bundle: Path, job: dict) -> None:
    """G15 for a done changeset, under the writer lock: close its work items and register the
    deferred Codex audit. Idempotent, so recovery can finish a job interrupted here."""
    try:
        closed = M.close_curated(bundle, list(job.get("work_items") or []), job_id=job["id"],
                                 commit=job.get("commit"), principal=str(job.get("principal")),
                                 run=job.get("run")) if job.get("close_items", True) else []
        job["audit"] = _register_audit(bundle, job)
        job["closed_items"] = closed
        job.pop("closeout_error", None)
    except Exception as exc:  # noqa: BLE001 — the commit is published; the next start retries this
        job["closeout_error"] = repr(exc)


def _register_audit(bundle: Path, job: dict) -> dict:
    """Phases 1-3: queue a Codex audit of the changeset; it waits out the maintainer run (§4.7).
    A bundle in AIWIKI_CODEX_AUDIT_MANUAL queues none: POST /jobs/{id}/audit requests it."""
    mode = os.environ.get("AIWIKI_AUDIT", "").strip() or "codex"
    if mode != "codex":
        return {"mode": mode}
    if bundle.name not in AUDIT_BUNDLES:
        return {"mode": "manual"}
    audit_job, existing = I.receive_audit(bundle, job["id"], audit.concept_files(bundle, job))
    if audit_job["status"] == "queued" and not existing:
        submit_audit(bundle, job["id"], I.job_path(bundle, audit_job["id"]))
    entry = {"mode": mode, "job": audit_job["id"]}
    if audit_job["status"] == "queued" and _maintaining(bundle):
        entry["deferred_until"] = "lease_release"
    return entry


def ensure_started() -> None:
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_run, name="curation-worker", daemon=True).start()
        _started = True


def _known_shas(bundle: Path) -> set[str]:
    """sha256 of every source any job already tracks (so the sweep never double-enqueues)."""
    shas: set[str] = set()
    jdir = bundle / ".okf" / "jobs"
    if not jdir.is_dir():
        return shas
    for jf in jdir.glob("*.json"):
        try:
            s = json.loads(jf.read_text(encoding="utf-8")).get("sha256")
        except (OSError, ValueError):
            continue
        if s:
            shas.add(s)
    return shas


def sweep_once(bundles: list[Path]) -> int:
    """Pick up sources sitting in sources/inbox/ that no job has seen yet (e.g. dropped
    out-of-band) and queue the curatable ones. Deduped by content sha. Returns #queued."""
    with serialized_lifecycle():
        queued = 0
        for b in bundles:
            inbox = b / "sources" / "inbox"
            if not inbox.is_dir():
                continue
            known = _known_shas(b)
            for f in sorted(inbox.iterdir()):
                if not f.is_file() or f.is_symlink() or f.name.startswith("."):
                    continue
                data = f.read_bytes()
                sha = hashlib.sha256(data).hexdigest()
                if sha in known:
                    continue
                source_rel = f.relative_to(b).as_posix()
                curatable = I.is_curatable(source_rel, data)
                job = I.new_job(b, source_rel, sha, curatable, filename=f.name)
                known.add(sha)
                if curatable:
                    submit(b, source_rel, I.job_path(b, job["id"]))
                    queued += 1
        return queued


def start_sweeper(bundles_fn) -> None:
    """Run sweep_once on a timer, in the background. bundles_fn() yields current bundle paths."""
    def _loop():
        while True:
            time.sleep(SWEEP_INTERVAL_S)
            try:
                sweep_once(bundles_fn())
            except Exception:  # noqa: BLE001 — a bad sweep must never kill the loop
                pass
    threading.Thread(target=_loop, name="inbox-sweeper", daemon=True).start()


def recover(bundles: list[Path]) -> bool:
    """Re-enqueue queued jobs and durably reconcile interrupted Git transactions."""
    queued: list[tuple[str, Path, str, Path]] = []
    closeouts: list[tuple[Path, Path]] = []
    pending_remote_confirmation = False
    for b in bundles:
        jdir = b / ".okf" / "jobs"
        if not jdir.is_dir():
            continue
        for jf in sorted(jdir.glob("*.json")):
            try:
                job = json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            status, kind = job.get("status"), job.get("kind", "ingest")
            if job.get("mode") == "changeset" and status != "queued":
                I.changeset_path(b, jf.stem).unlink(missing_ok=True)  # only a queued job runs from it
            if status == "queued" and kind == "audit" and job.get("parent_job"):
                queued.append(("audit", b, job["parent_job"], jf))
            elif status == "queued" and kind == "revert":
                queued.append(("revert", b, jf.stem, jf))
            elif status == "queued" and job.get("mode") == "changeset":
                if I.changeset_path(b, jf.stem).is_file():
                    queued.append(("changeset", b, jf.stem, jf))
                else:
                    job.update(status="failed", error="changeset request lost before it ran; resubmit it",
                               finished=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               failure=failure("interrupted", stage="startup", detail="changeset request lost"))
                    _save_job(jf, job)
            elif status == "done" and job.get("mode") == "changeset" and "closed_items" not in job:
                closeouts.append((b, jf))  # G15 interrupted after the receipt read done
            elif status == "queued" and job.get("source") and job.get("mode") != "changeset":
                # A changeset stages its packet as ``source``; the Codex curator never takes it.
                queued.append(("ingest", b, job["source"], jf))
            elif status == "running":
                outcome = _reconcile_running(b, job)
                if outcome == RECOVERY_PENDING:
                    _save_job(jf, job)
                    pending_remote_confirmation = True
                    continue
                if job.get("status") != "done":
                    job["status"] = "failed"
                    job["error"] = f"interrupted by service restart: {outcome}"
                    # Only a confirmed rollback makes a fresh attempt safe.
                    rolled_back = job.get("phase") == "rolled_back"
                    job["failure"] = failure(
                        "interrupted" if rolled_back else "internal", stage="startup",
                        detail=job["error"], retryable=None if rolled_back else False,
                    )
                    if kind == "revert":
                        job["reverted"] = []  # its commit never reached the remote
                job["finished"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                _save_job(jf, job)
                curate._cleanup_recovery_source(b, job)
                if job.get("status") == "done" and job.get("mode") == "changeset":
                    closeouts.append((b, jf))  # the remote holds the commit: finish G15
    # Never build new work on an unresolved local commit. A failed fetch is retried
    # on the next service startup rather than being mistaken for a rejected push.
    if pending_remote_confirmation:
        return False
    # Reconciliation can reset a repository. Queue prior work only after *every*
    # interrupted transaction is terminal, and startup starts the thread after this
    # function returns, so recovery never races a new mutation.
    for bundle, job_path in closeouts:
        job = json.loads(job_path.read_text(encoding="utf-8"))
        _closeout(bundle, job)
        _save_job(job_path, job)
    for kind, bundle, subject, job_path in queued:
        if kind == "audit":
            submit_audit(bundle, subject, job_path)
        elif kind == "changeset":
            submit_changeset(bundle, job_path)
        elif kind == "revert":
            submit_revert(bundle, job_path)
        else:
            submit(bundle, subject, job_path)
    return True
