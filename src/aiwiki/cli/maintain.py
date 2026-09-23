#!/usr/bin/env python3
"""Resume a source manifest through the installed ai-wiki CLI. Never edit a bundle.

The state directory is durable, single-writer orchestration state, NOT an OKF bundle or
reference repo. Reuse it across daily runs; archive state.json with the run report.
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


class Pending(RuntimeError):
    """Keep this source pending without discarding independent progress."""

    def __init__(self, message: str, *, rejected: bool = False):
        super().__init__(message)
        self.rejected = rejected


def cli(*args: str, read: bool = False) -> dict:
    """Only retry known transient reads. An uncertain write must be reconciled by ID."""
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


def retry_kind(job: dict) -> str | None:
    # A terminal, rolled-back attempt is required before starting another model pass.
    if job.get("status") != "failed" or job.get("phase") != "rolled_back":
        return None
    if job.get("validation", {}).get("status") == "failed":
        return None
    error = str(job.get("error", "")) + " " + str(job.get("git", {}).get("note", ""))
    if re.search(r"\b(auth(?:entication|orization)?|login|unauthorized|forbidden|permission denied|"
                 r"disk full|no space left)\b|\bstatus\s*[:=]?\s*(401|403)\b", error, re.I):
        return None
    if re.search(r"\busage limit\b|\binsufficient_quota\b|\bquota (?:exceeded|exhausted)\b|"
                 r"\brate[ _-]limit(?:ed|_exceeded)?\b|too many requests|\b(?:HTTP|status)\s*[:=]?\s*429\b",
                 error, re.I):
        return "capacity"
    if re.search(r"\btimeout\b|timed out|\b(?:HTTP|status)\s*[:=]?\s*5\d\d\b|"
                 r"connection (?:error|reset|refused|failed|closed)|network (?:error|unreachable)|"
                 r"temporarily unavailable|rebase conflict", error, re.I):
        return "transient"
    return None


def retry_plan(entry: dict, stage: str, job: dict, kind: str) -> dict:
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
        plan = {"job": job["id"], "stage": stage, "kind": kind,
                "after": datetime.fromtimestamp(since + (3600 if kind == "capacity" else 300), UTC)
                .isoformat(timespec="seconds").replace("+00:00", "Z")}
        entry["retry"] = plan
    return plan


def summary(state: dict) -> dict:
    rows = []
    for entry in state["sources"]:
        row = {k: entry[k] for k in ("identity", "sha256", "status", "error", "retry") if k in entry}
        retry = entry.get("retry") or state.get("writer_retry")
        row["action"] = ("done" if entry["status"] == "done" else "wait_then_resume" if retry
                         else "repair" if entry.get("error") else "resume")
        if retry and entry["status"] != "done":
            row["retry_at"] = retry["after"]
        rows.append(row)
    return {"done": sum(s["status"] == "done" for s in state["sources"]),
            "pending": sum(s["status"] != "done" for s in state["sources"]),
            **({"writer_retry": state["writer_retry"]} if state.get("writer_retry") else {}), "sources": rows}


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
            job = cli("-b", bundle, "jobs", job_id, "--json", read=True)
            if job.get("id") != job_id:
                raise ValueError("imported job ID mismatch")
            if stage == "ingest" and (job.get("kind", "ingest") != "ingest" or job.get("sha256") != sha):
                raise ValueError("imported ingest does not match frozen evidence")
            if stage == "audit" and (not entry["ingest"] or job.get("parent_job") != entry["ingest"][-1]["id"]):
                raise ValueError("imported audit parent mismatch")
            entry[stage].append(job)
            if entry.get("submitting") == stage:
                entry.pop("submitting")


def run_sources(state: dict, path: Path, bundle: str, *, poll: float = 15, wait: float = 3600,
                retry_now: bool = False) -> dict:
    cooldown = state.get("writer_retry")
    if cooldown and not retry_now and datetime.fromisoformat(cooldown["after"]).timestamp() > time.time():
        return summary(state)
    state.pop("writer_retry", None)
    blocked = set()
    for entry in state["sources"]:
        if entry["status"] == "done":
            continue
        identity = entry["identity"]
        entry.pop("error", None)
        try:
            if identity in blocked:
                raise Pending("an earlier version of this source is unfinished")
            if entry.get("submitting"):
                raise Pending("submission outcome unknown; import the existing job ID before retrying")
            if entry.get("path") and hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest() != entry["sha256"]:
                raise Pending("frozen source hash mismatch")
            for stage in ("ingest", "audit"):
                deadline = time.monotonic() + wait
                parent = entry["ingest"][-1]["id"] if stage == "audit" else None
                submitted = False  # at most one new model attempt per stage per invocation
                while True:
                    attempts = entry[stage]
                    job = attempts[-1] if attempts else None
                    if job is None or job.get("status") == "failed":
                        if job is not None:
                            if job.get("kind", "ingest") != stage or (
                                job.get("sha256") != entry["sha256"] if stage == "ingest"
                                else job.get("parent_job") != parent
                            ):
                                raise Pending("failed job does not match source/stage/parent")
                            kind = retry_kind(job)
                            if kind is None:
                                entry.pop("retry", None)
                                raise Pending(f"{stage} requires repair: {job.get('error', 'unsafe retry')}")
                            plan = retry_plan(entry, stage, job, kind)
                            if submitted or (not retry_now and
                                             datetime.fromisoformat(plan["after"]).timestamp() > time.time()):
                                if kind == "capacity":
                                    state["writer_retry"] = dict(plan)
                                raise Pending(f"{stage} {kind} failure; rerun after {plan['after']}")
                        entry["submitting"] = stage
                        save(path, state)  # crash/unknown POST is not permission to launch another job
                        if stage == "ingest":
                            result = cli("-b", bundle, "ingest", entry["path"], "--title", identity, "--json")
                            job = {"id": result["submissions"][0]["job"], "status": "queued"}
                        else:
                            job = cli("-b", bundle, "audit", parent, "--json")
                        if not isinstance(job.get("id"), str) or not job["id"]:
                            raise Pending("submission response missing job ID")
                        attempts.append(job)
                        submitted = True
                        entry.pop("retry", None)
                        entry.pop("submitting")
                        save(path, state)
                    if job.get("status") not in {"done", "failed", "needs-conversion"}:
                        updated = cli("-b", bundle, "jobs", job["id"], "--json", read=True)
                        if updated.get("id") != job["id"]:
                            raise Pending("job ID mismatch")
                        job = updated
                        attempts[-1] = job
                        save(path, state)
                    if stage == "ingest" and job.get("status") in {"done", "failed", "needs-conversion"}:
                        if job.get("sha256") != entry["sha256"] or job.get("kind", "ingest") != "ingest":
                            raise Pending("ingest receipt source identity mismatch")
                    if job.get("status") == "done":
                        receipt(job, parent=parent)
                        break
                    if job.get("status") == "failed":
                        continue
                    if job.get("status") == "needs-conversion":
                        raise Pending("evidence needs conversion; submit a readable artifact")
                    if job.get("status") not in {"queued", "running"}:
                        raise Pending("unknown job state")
                    if time.monotonic() >= deadline:
                        raise Pending("poll deadline reached; retain job ID and resume, do not resubmit")
                    time.sleep(poll)
            entry["status"] = "done"
            entry.pop("retry", None)
            entry["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        except (Pending, OSError, KeyError, ValueError) as exc:
            if isinstance(exc, Pending) and exc.rejected:
                entry.pop("submitting", None)
            entry["status"] = "pending"
            entry["error"] = str(exc)
            blocked.add(identity)
        save(path, state)
        if state.get("writer_retry"):
            break  # account-wide rejection: do not submit independent sources to the same writer
    return summary(state)


def run(*, manifest: Path | None, state_dir: Path, bundle: str, audit_pending: bool = False,
        poll: float = 15, wait: float = 3600, retry_now: bool = False) -> dict:
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
        if manifest is not None:
            add_sources(state, json.loads(manifest.read_text()), state_dir, bundle)
            save(state_path, state)  # freeze new evidence even if orphan discovery is unavailable
        if audit_pending:
            pending = cli("-b", bundle, "jobs", "--pending-audit", "--json", read=True)
            known = {job["id"] for entry in state["sources"] for job in entry["ingest"]}
            for job in pending["jobs"]:
                if job["id"] not in known:
                    receipt(job)
                    state["sources"].append({
                        "identity": "pending-audit:" + job["id"], "sha256": job["sha256"],
                        "ingest": [job], "audit": job.get("failed_audit_attempts", []), "status": "pending",
                    })
            state["pending_audit_discovery"] = {k: pending[k] for k in ("shown", "total", "truncated", "unscoped")}
        save(state_path, state)
        return run_sources(state, state_path, bundle, poll=poll, wait=wait, retry_now=retry_now)
