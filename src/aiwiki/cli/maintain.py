#!/usr/bin/env python3
"""Resume a source manifest through the installed ai-wiki CLI. Never edit a bundle.

The state directory is durable, single-writer orchestration state, NOT an OKF bundle or
reference repo. Reuse it across daily runs; archive a copy of state.json with the run report
and never move, delete or edit the original.

Each run leaves every entry in one status:

- ``done``: ingest and audit receipts are complete;
- ``pending``: a later run retries it (``retry.after`` when a cooldown applies);
- ``needs_repair``: attempt cap exhausted or not retryable; never blocks other identities;
- ``superseded``: a newer version of the same identity replaced this unfinished one;
- ``dropped``: an operator abandoned it with ``--drop`` and a reason (receipts kept).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from aiwiki.runtime.failure import classify

STATUSES = ("done", "pending", "needs_repair", "superseded", "dropped")
# Failed attempts per (source, stage) before an entry needs repair. Capacity failures do
# not count: they cool down for an hour and stop the batch instead.
ATTEMPT_CAPS = {"transient": 3, "timeout": 3, "interrupted": 3, "conflict": 3, "model_output": 3,
                "internal": 2}
# Consecutive capacity failures of one stage that may stop the batch. Past this, the entry
# cools down on its own so a misclassified "capacity" can never starve independent sources.
CAPACITY_BATCH_STOPS = 3
# Done audits whose reviewer verdict was missing/invalid; mirrors the writer's per-parent bound.
MAX_VERDICT_SLIPS = 2


def _verdict_slip(job: dict) -> bool:
    audit = job.get("audit") if isinstance(job.get("audit"), dict) else {}
    return job.get("status") == "done" and audit.get("reason") in {"verdict_missing", "verdict_invalid"}


class Pending(RuntimeError):
    """Keep this source pending without discarding independent progress."""

    def __init__(self, message: str, *, rejected: bool = False):
        super().__init__(message)
        self.rejected = rejected


class NeedsRepair(RuntimeError):
    """Stop retrying this source until new evidence, a deployed fix, or an imported receipt."""


def cli(*args: str, read: bool = False) -> dict:
    """Only retry known transient reads. An uncertain write is re-POSTed by the next run."""
    for attempt in range(4):
        rejected = False
        try:
            proc = subprocess.run([sys.executable, "-m", "aiwiki.cli.main", *args],
                                  capture_output=True, text=True, timeout=45)
            if proc.returncode == 0:
                return json.loads(proc.stdout)
            error = (proc.stdout + proc.stderr).strip()
            rejected = proc.returncode == 2 or bool(re.search(r"status 4\d\d", error))
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            error = str(exc)
        transient = bool(re.search(r"status (?:429|5\d\d)|cannot reach|timed out", error))
        if not read or not transient or attempt == 3:
            raise Pending(error[:2000], rejected=rejected)
        time.sleep((5, 15, 30)[attempt])
    raise AssertionError("unreachable")


def save(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(state, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def receipt(job: dict, *, parent: str | None = None) -> None:
    if job.get("status") != "done" or job.get("validation", {}).get("status") != "passed":
        raise Pending("job is not a validated done receipt")
    if parent is not None:
        if job.get("parent_job") != parent or job.get("kind") != "audit":
            raise Pending("audit parent/kind mismatch")
        audit = job.get("audit", {})
        if audit.get("status") not in {"passed", "needs_attention"}:
            raise Pending("audit has no terminal business result")
        for key in ("verified_concepts", "unverified_concepts", "corrected_concepts"):
            if not isinstance(audit.get(key), list):
                raise Pending(f"audit receipt missing {key}")
    git = job.get("git", {})
    commit = job.get("commit")
    if commit:
        if git.get("committed") is not True or git.get("commit") != commit or git.get("pushed") is not True:
            raise Pending("remote writer receipt lacks matching committed/pushed revision")
    elif parent is not None and job.get("reason") == "no_concepts_to_audit":
        if job.get("changed_files") != [] or job["audit"].get("status") != "passed":
            raise Pending("invalid no-concept exception")
        if any(job["audit"][key] for key in ("verified_concepts", "unverified_concepts", "corrected_concepts")):
            raise Pending("no-concept receipt contains concepts")
    elif git.get("note") != "no changes" or job.get("changed_files") != []:
        raise Pending("receipt lacks Git result or documented no-change exception")


def retry_kind(job: dict) -> str:
    """The failure class: the writer's ``job.failure`` when present, else the legacy fallback."""
    return classify(job)["class"]


def retry_plan(entry: dict, stage: str, job: dict, failure: dict) -> dict:
    """Keep a durable cooldown; do not guess a timezone from a provider's prose reset date."""
    plan = entry.get("retry", {})
    if plan.get("job") != job["id"] or plan.get("stage") != stage:
        try:
            finished = datetime.fromisoformat(job["finished"])
            if finished.tzinfo is None:
                raise ValueError("timestamp needs timezone")
            since = finished.timestamp()
        except (KeyError, ValueError, TypeError):
            since = time.time()
        delay = failure.get("retry_after_s")
        plan = {"job": job["id"], "stage": stage, "kind": failure["class"],
                "after": datetime.fromtimestamp(since + (delay if isinstance(delay, int) else 300), UTC)
                .isoformat(timespec="seconds").replace("+00:00", "Z")}
        entry["retry"] = plan
    return plan


def summary(state: dict) -> dict:
    rows = []
    for entry in state["sources"]:
        row = {k: entry[k] for k in ("identity", "sha256", "status", "error", "retry", "superseded_by", "dropped")
               if k in entry}
        retry = entry.get("retry") or state.get("writer_retry")
        if entry["status"] == "pending":
            row["action"] = "wait_then_resume" if retry else "resume"
            if retry:
                row["retry_at"] = retry["after"]
        else:
            row["action"] = {"done": "done", "needs_repair": "repair"}.get(entry["status"], "none")
        rows.append(row)
    counts = {status: sum(entry["status"] == status for entry in state["sources"]) for status in STATUSES}
    return {**counts, **({"writer_retry": state["writer_retry"]} if state.get("writer_retry") else {}),
            **({"warnings": state["warnings"]} if state.get("warnings") else {}), "sources": rows}


def exit_code(result: dict) -> int:
    """0: all done/superseded/dropped; 1: pending or a read to redo next run; 3: some need repair."""
    return 3 if result.get("needs_repair") else 1 if result.get("pending") or result.get("warnings") else 0


def add_sources(state: dict, manifest: dict, directory: Path, bundle: str) -> None:
    """Freeze evidence before submission and retain unfinished sources across manifests."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("sources"), list):
        raise ValueError("manifest must be an object with a sources array")
    for source in manifest["sources"]:
        identity = source["identity"]
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("source identity must be a nonempty string")
        data = Path(source["path"]).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if source.get("sha256", sha) != sha:
            raise ValueError(f"source hash mismatch: {identity}")
        entry = next((row for row in state["sources"] if row["identity"] == identity and row["sha256"] == sha), None)
        if entry is None:
            # Keep the hash in the durable directory, not the submitted basename:
            # the writer adds its own hash to source snapshot filenames.
            frozen = directory / "sources" / sha / ("evidence" + Path(source["path"]).suffix)
            frozen.parent.mkdir(parents=True, exist_ok=True)
            if frozen.exists() and frozen.read_bytes() != data:
                raise ValueError("frozen source was modified")
            frozen.write_bytes(data)
            entry = {"identity": identity, "sha256": sha, "path": str(frozen.resolve()),
                     "ingest": [], "audit": [], "status": "pending"}
            state["sources"].append(entry)
        # Import prior receipts / resolve an uncertain submission. Never trust just an ID
        # or overwrite a recorded attempt; fetch and check its source/parent identity.
        for stage in ("ingest", "audit"):
            job_id = source.get(stage + "_job")
            if not job_id or any(j["id"] == job_id for j in entry[stage]):
                continue
            try:
                job = cli("-b", bundle, "jobs", job_id, "--json", read=True)
            except Pending as exc:
                # Keep freezing the manifest. Processing this entry re-POSTs, which the writer
                # dedupes onto that job; the import itself is retried while the manifest lists it.
                state.setdefault("warnings", []).append(f"{identity}: import of {stage} job {job_id} "
                                                        f"unavailable: {exc}"[:500])
                break
            if job.get("id") != job_id:
                raise ValueError("imported job ID mismatch")
            if stage == "ingest" and (job.get("kind", "ingest") != "ingest" or job.get("sha256") != sha):
                raise ValueError("imported ingest does not match frozen evidence")
            if stage == "audit" and (not entry["ingest"] or job.get("parent_job") != entry["ingest"][-1]["id"]):
                raise ValueError("imported audit parent mismatch")
            entry[stage].append(job)
            if entry.get("submitting") == stage:
                entry.pop("submitting")
            if entry["status"] in {"superseded", "needs_repair", "dropped"}:
                entry["status"] = "pending"  # re-evaluate with the imported receipt on the next run
                entry.pop("superseded_by", None)
                entry.pop("dropped", None)


def _submit(state: dict, path: Path, bundle: str, entry: dict, stage: str, parent: str | None) -> dict:
    """POST one attempt, recording the intent first.

    The writer is idempotent for both POSTs: identical ingest bytes return the existing
    queued/running/done job (service/ingest.py find_job_by_sha), and an audit POST returns
    the queued/running/done attempt for that parent (find_audit_job). Re-POSTing after an
    unknown outcome therefore adopts the job it created instead of launching a duplicate.
    """
    if stage == "ingest":
        if not entry.get("path"):
            raise NeedsRepair("no frozen evidence to submit")
        try:
            data = Path(entry["path"]).read_bytes()
        except OSError as exc:
            raise NeedsRepair(f"frozen source unreadable: {exc}") from None
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise NeedsRepair("frozen source hash mismatch")
    entry["submitting"] = stage
    save(path, state)  # an unknown POST outcome is reconciled by re-POSTing, never duplicated
    if stage == "ingest":
        result = cli("-b", bundle, "ingest", entry["path"], "--title", entry["identity"], "--json")
        job = {"id": result["submissions"][0]["job"], "status": "queued"}
    else:
        job = cli("-b", bundle, "audit", parent, "--json")
    if not isinstance(job.get("id"), str) or not job["id"]:
        raise Pending("submission response missing job ID")
    attempts = entry[stage]
    if attempts and attempts[-1].get("id") == job["id"]:
        attempts[-1] = job
    else:
        attempts.append(job)
    entry.pop("retry", None)
    entry.pop("submitting")
    save(path, state)
    return job


def _extra_build(entry: dict, stage: str, job: dict, writer_build) -> str | None:
    """The reported build that earns one attempt past the cap: a fix may have been deployed.

    /health comes from the read mirror, which may stay on another build than the writer that
    stamps receipts, so each reported build earns at most one attempt (``build_retry``).
    """
    service = job.get("service") if isinstance(job.get("service"), dict) else {}
    build = service.get("build")
    if not isinstance(build, str) or not build:
        return None
    current = writer_build()
    if current is None or current.startswith(build) or build.startswith(current):  # short or full SHA
        return None
    return None if entry.get("build_retry", {}).get(stage) == current else current


def _retry_failure(entry: dict, stage: str, parent: str | None, job: dict, writer_build) -> dict:
    """Return the failure record when a fresh attempt is allowed, else raise NeedsRepair."""
    if job.get("kind", "ingest") != stage or (
        job.get("sha256") != entry["sha256"] if stage == "ingest" else job.get("parent_job") != parent
    ):
        raise NeedsRepair("failed job does not match source/stage/parent")
    failure = classify(job)
    label = f"{stage} {failure['class']} failure"
    detail = f": {failure['detail']}" if failure["detail"] else ""
    if not failure["retryable"]:
        raise NeedsRepair(f"{label} is not retryable{detail}")
    # A terminal, rolled-back attempt is required before starting another model pass.
    if job.get("phase") != "rolled_back":
        raise NeedsRepair(f"{label} without a confirmed rollback{detail}")
    cap = ATTEMPT_CAPS.get(failure["class"])
    if cap is not None:
        failed = sum(1 for attempt in entry[stage]
                     if attempt.get("status") == "failed" and classify(attempt)["class"] != "capacity")
        if failed >= cap:
            failure["extra_build"] = _extra_build(entry, stage, job, writer_build)
            if failure["extra_build"] is None:
                raise NeedsRepair(f"{label}; {failed} failed attempts reached the cap of {cap}{detail}")
    return failure


def _process(state: dict, path: Path, bundle: str, entry: dict, newer: dict | None, *,
             poll: float, wait: float, retry_now: bool, writer_build) -> None:
    for stage in ("ingest", "audit"):
        deadline = time.monotonic() + wait
        parent = entry["ingest"][-1]["id"] if stage == "audit" else None
        submitted = False  # at most one new model attempt per stage per invocation
        while True:
            attempts = entry[stage]
            job = attempts[-1] if attempts else None
            if stage == "ingest" and newer is not None and all(
                    attempt.get("status") == "needs-conversion"
                    or (attempt.get("status") == "failed" and attempt.get("phase") == "rolled_back")
                    for attempt in attempts):
                # The newer version carries this identity forward; keep every receipt. A lost
                # POST (``submitting``) is dropped too: a job it did create stays on the writer,
                # and --audit-pending adopts it if it finishes. An attempt without a confirmed
                # rollback may have landed unaudited, so it goes to repair instead.
                entry["status"] = "superseded"
                entry["superseded_by"] = newer["sha256"]
                entry.pop("retry", None)
                entry.pop("submitting", None)
                return
            if entry.get("submitting") == stage or job is None:
                job = _submit(state, path, bundle, entry, stage, parent)
                submitted = True
            elif job.get("status") == "failed":
                failure = _retry_failure(entry, stage, parent, job, writer_build)
                plan = retry_plan(entry, stage, job, failure)
                if submitted or (not retry_now and
                                 datetime.fromisoformat(plan["after"]).timestamp() > time.time()):
                    if failure["class"] == "capacity":
                        streak = next((n for n, attempt in enumerate(reversed(attempts))
                                       if attempt.get("status") != "failed"
                                       or classify(attempt)["class"] != "capacity"), len(attempts))
                        if streak <= CAPACITY_BATCH_STOPS:
                            state["writer_retry"] = dict(plan)
                    raise Pending(f"{stage} {failure['class']} failure; rerun after {plan['after']}")
                if failure.get("extra_build"):
                    # Spent only when the attempt is actually made; _submit persists it first.
                    entry.setdefault("build_retry", {})[stage] = failure["extra_build"]
                job = _submit(state, path, bundle, entry, stage, parent)
                submitted = True
            if job.get("status") not in {"done", "failed", "needs-conversion"}:
                updated = cli("-b", bundle, "jobs", job["id"], "--json", read=True)
                if updated.get("id") != job["id"]:
                    raise Pending("job ID mismatch")
                job = updated
                attempts[-1] = job
                save(path, state)
            if stage == "ingest" and job.get("status") in {"done", "failed", "needs-conversion"}:
                if job.get("sha256") != entry["sha256"] or job.get("kind", "ingest") != "ingest":
                    raise NeedsRepair("ingest receipt source identity mismatch")
            if job.get("status") == "done":
                try:
                    receipt(job, parent=parent)
                except Pending as exc:
                    raise NeedsRepair(f"{stage} receipt rejected: {exc}") from None
                if (stage == "audit" and _verdict_slip(job)
                        and sum(map(_verdict_slip, attempts)) < MAX_VERDICT_SLIPS):
                    # The reviewer omitted or garbled its verdict: a format slip, not an evidence
                    # judgment. The writer grants one fresh review per parent, then dedupes, so
                    # this bound (not the one-attempt-per-invocation rule) keeps it finite.
                    job = _submit(state, path, bundle, entry, stage, parent)
                    submitted = True
                    continue
                break
            if job.get("status") == "failed":
                continue
            if job.get("status") == "needs-conversion":
                raise NeedsRepair("evidence needs conversion; submit a readable artifact")
            if job.get("status") not in {"queued", "running"}:
                raise NeedsRepair("unknown job state")
            if time.monotonic() >= deadline:
                raise Pending("poll deadline reached; retain job ID and resume, do not resubmit")
            time.sleep(poll)
    entry["status"] = "done"
    entry.pop("retry", None)
    entry["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_sources(state: dict, path: Path, bundle: str, *, poll: float = 15, wait: float = 3600,
                retry_now: bool = False) -> dict:
    cooldown = state.get("writer_retry")
    if cooldown and not retry_now and datetime.fromisoformat(cooldown["after"]).timestamp() > time.time():
        return summary(state)
    state.pop("writer_retry", None)
    writer: dict = {}

    def writer_build() -> str | None:
        # Only consulted at an attempt cap. The public /health may be served by a read
        # mirror that reports no build; unknown means no extra attempt.
        if "build" not in writer:
            try:
                build = cli("-b", bundle, "health", "--json").get("build")
            except Pending:
                build = None
            writer["build"] = build if isinstance(build, str) and build else None
        return writer["build"]

    latest = {entry["identity"]: entry for entry in state["sources"]}
    blocked = set()
    for entry in state["sources"]:
        if entry["status"] in {"done", "superseded", "dropped"}:
            continue
        identity = entry["identity"]
        entry.pop("error", None)
        try:
            if identity in blocked:
                raise Pending("an earlier version of this source is unfinished")
            _process(state, path, bundle, entry, None if latest[identity] is entry else latest[identity],
                     poll=poll, wait=wait, retry_now=retry_now, writer_build=writer_build)
        except NeedsRepair as exc:
            entry["status"] = "needs_repair"
            entry["error"] = str(exc)
            entry.pop("retry", None)
        except (Pending, OSError, KeyError, ValueError) as exc:
            entry["status"] = "pending"
            entry["error"] = str(exc)
            if isinstance(exc, Pending) and exc.rejected:
                entry.pop("submitting", None)
                if not re.search(r"status 429|in progress", str(exc)):
                    # The writer answered and refused; resubmitting the same request cannot help.
                    entry["status"] = "needs_repair"
                    entry.pop("retry", None)
            if entry["status"] == "pending":
                blocked.add(identity)  # newer versions of this identity wait behind it
        save(path, state)
        if state.get("writer_retry"):
            break  # account-wide rejection: do not submit independent sources to the same writer
    return summary(state)


def _discover_pending_audits(state: dict, bundle: str) -> None:
    """Adopt orphaned done ingests; one malformed orphan never aborts the run."""
    try:
        pending = cli("-b", bundle, "jobs", "--pending-audit", "--json", read=True)
    except Pending as exc:
        # Orphans stay listed on the writer and the next run finds them; never block the manifest.
        state.setdefault("warnings", []).append(f"pending-audit discovery unavailable: {exc}"[:500])
        return
    known = {job.get("id") for entry in state["sources"] for job in entry["ingest"]}
    invalid = []
    for job in pending.get("jobs", []):
        if job.get("id") in known:
            continue
        try:
            receipt(job)
            sha = job["sha256"]
        except (Pending, KeyError) as exc:
            invalid.append({"id": job.get("id"), "error": str(exc)})
            continue
        state["sources"].append({
            "identity": "pending-audit:" + job["id"], "sha256": sha,
            "ingest": [job], "audit": job.get("failed_audit_attempts", []), "status": "pending",
        })
    discovery = {k: pending.get(k) for k in ("shown", "total", "truncated", "unscoped")}
    if invalid:
        discovery["invalid"] = invalid
    state["pending_audit_discovery"] = discovery


def status(state_dir: Path) -> dict:
    """Offline summary of the saved state: no lock, no network."""
    state_path = state_dir / "state.json"
    if not state_path.exists():
        raise ValueError("no saved state in this --state-dir")
    return summary(json.loads(state_path.read_text()))


def drop(state_dir: Path, sha_prefix: str, reason: str) -> dict:
    """Abandon one unfinished source for good; its receipts stay for the record.

    The only exit from ``needs_repair`` that needs no new evidence or build: newer versions
    of the identity stop waiting behind it and exit code 3 clears.
    """
    if not reason.strip():
        raise ValueError("--drop needs a --reason")
    if len(sha_prefix) < 8:
        raise ValueError("--drop needs at least 8 hex characters of the source sha256")
    state_path = state_dir / "state.json"
    if not state_path.exists():
        raise ValueError("no saved state in this --state-dir")
    with (state_dir / "runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Pending("another runner owns this state directory") from None
        state = json.loads(state_path.read_text())
        matches = [entry for entry in state["sources"] if entry["sha256"].startswith(sha_prefix.lower())]
        if len(matches) != 1:
            raise ValueError(f"--drop {sha_prefix} matches {len(matches)} sources; give a longer prefix")
        entry = matches[0]
        if entry["status"] not in {"pending", "needs_repair"}:
            raise ValueError(f"only pending or needs_repair sources can be dropped, not {entry['status']}")
        if entry.get("submitting"):
            raise ValueError("a submission is in flight; let the next run reconcile it first")
        entry["status"] = "dropped"
        entry["dropped"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reason": reason.strip()[:500]}
        entry.pop("retry", None)
        save(state_path, state)
        return summary(state)


def run(*, manifest: Path | None, state_dir: Path, bundle: str, audit_pending: bool = False,
        poll: float = 15, wait: float = 3600, retry_now: bool = False, import_only: bool = False) -> dict:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Pending("another runner owns this state directory") from None
        config = cli("config", "show", "--json", read=True)
        endpoint = config["endpoint"].rstrip("/")
        state_path = state_dir / "state.json"
        if manifest is None and not state_path.exists():
            raise ValueError("no saved state; provide --manifest for the first run")
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "version": 1, "endpoint": endpoint, "bundle": bundle, "sources": []}
        if (state.get("version"), state.get("endpoint"), state.get("bundle")) != (1, endpoint, bundle):
            raise ValueError("state belongs to a different endpoint/bundle/version")
        state["warnings"] = []  # reads this run could not do; the summary reports the last run
        if manifest is not None:
            add_sources(state, json.loads(manifest.read_text()), state_dir, bundle)
            save(state_path, state)  # freeze new evidence even if orphan discovery is unavailable
        if audit_pending:
            _discover_pending_audits(state, bundle)
        save(state_path, state)
        if import_only:
            return summary(state)
        return run_sources(state, state_path, bundle, poll=poll, wait=wait, retry_now=retry_now)
