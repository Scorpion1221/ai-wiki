"""Serial curation worker.

Ingest only *queues* work; a single background thread drains the queue one job at a
time. Serializing curation is what makes many concurrent writers safe: two curation
passes never touch the bundle or its git tree at once, so the only contention left is
between this worker and *other* writers' pushes — which curate.py handles by rebasing.

On startup, queued jobs left by a previous run are re-enqueued. Interrupted running
jobs are reconciled from durable Git transaction metadata before being marked failed.
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..runtime import audit, curate
from . import ingest as I

_q: queue.Queue = queue.Queue()
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


def submit(bundle: Path, source_rel: str, job_path: Path) -> None:
    _q.put(("ingest", bundle, source_rel, job_path))


def submit_audit(bundle: Path, parent_job: str, job_path: Path) -> None:
    _q.put(("audit", bundle, parent_job, job_path))


def _run() -> None:
    while True:
        kind, bundle, subject, job_path = _q.get()
        try:
            with serialized_mutation():
                if kind == "audit":
                    audit.run(bundle, subject, job_path)
                else:
                    curate.run(bundle, subject, job_path)
        except Exception:  # noqa: BLE001 — runtimes record their own failures; never kill the worker
            pass
        finally:
            _q.task_done()


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
            if status == "queued" and kind == "audit" and job.get("parent_job"):
                queued.append(("audit", b, job["parent_job"], jf))
            elif status == "queued" and job.get("source"):
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
                job["finished"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                _save_job(jf, job)
                curate._cleanup_recovery_source(b, job)
    # Never build new work on an unresolved local commit. A failed fetch is retried
    # on the next service startup rather than being mistaken for a rejected push.
    if pending_remote_confirmation:
        return False
    # Reconciliation can reset a repository. Queue prior work only after *every*
    # interrupted transaction is terminal, and startup starts the thread after this
    # function returns, so recovery never races a new mutation.
    for kind, bundle, subject, job_path in queued:
        if kind == "audit":
            submit_audit(bundle, subject, job_path)
        else:
            submit(bundle, subject, job_path)
    return True
