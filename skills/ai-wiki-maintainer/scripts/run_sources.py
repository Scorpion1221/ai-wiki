#!/usr/bin/env python3
"""Resume a source manifest through the installed ai-wiki CLI. Never edit a bundle.

The state directory is durable, single-writer orchestration state, NOT an OKF bundle or
reference repo. Reuse it across daily runs; archive state.json with the run report.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
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
            proc = subprocess.run(["ai-wiki", *args], capture_output=True, text=True, timeout=45)
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


def retryable(job: dict) -> bool:
    # A terminal, rolled-back attempt is required before starting another model pass.
    if job.get("status") != "failed" or job.get("phase") != "rolled_back":
        return False
    if job.get("validation", {}).get("status") == "failed":
        return False
    error = str(job.get("error", "")) + " " + str(job.get("git", {}).get("note", ""))
    if re.search(r"auth|login|permission|disk|space|401|403", error, re.I):
        return False
    return bool(re.search(r"timeout|timed out|429|5\d\d|connection|rebase conflict", error, re.I))


def add_sources(state: dict, manifest: dict, directory: Path, bundle: str) -> None:
    """Freeze evidence before submission and retain unfinished sources across manifests."""
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
            frozen = directory / "sources" / (sha + Path(source["path"]).suffix)
            frozen.parent.mkdir(exist_ok=True)
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


def run_sources(state: dict, path: Path, bundle: str, *, poll: float = 15, wait: float = 3600) -> dict:
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
                while True:
                    attempts = entry[stage]
                    job = attempts[-1] if attempts else None
                    if job is None or job.get("status") == "failed":
                        if job is not None and (len(attempts) >= 2 or not retryable(job)):
                            raise Pending(f"{stage} requires repair: {job.get('error', 'retry budget exhausted')}")
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
            entry["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        except (Pending, OSError, KeyError, ValueError) as exc:
            if isinstance(exc, Pending) and exc.rejected:
                entry.pop("submitting", None)
            entry["status"] = "pending"
            entry["error"] = str(exc)
            blocked.add(identity)
        save(path, state)
    return {"done": sum(s["status"] == "done" for s in state["sources"]),
            "pending": sum(s["status"] != "done" for s in state["sources"]),
            "sources": [{k: s[k] for k in ("identity", "sha256", "status", "error") if k in s}
                        for s in state["sources"]]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--audit-pending", action="store_true",
                        help="also close up to 20 orphaned ingests older than 24h")
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--wait-seconds", type=float, default=3600)
    args = parser.parse_args(argv)
    if args.poll_seconds < 0 or args.wait_seconds <= 0:
        parser.error("poll must be nonnegative and wait must be positive")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / "runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Pending("another runner owns this state directory") from None
        config = cli("config", "show", "--json", read=True)
        endpoint = config["endpoint"].rstrip("/")
        state_path = args.state_dir / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "version": 1, "endpoint": endpoint, "bundle": args.bundle, "sources": []}
        if (state.get("version"), state.get("endpoint"), state.get("bundle")) != (1, endpoint, args.bundle):
            raise ValueError("state belongs to a different endpoint/bundle/version")
        add_sources(state, json.loads(args.manifest.read_text()), args.state_dir, args.bundle)
        if args.audit_pending:
            pending = cli("-b", args.bundle, "jobs", "--pending-audit", "--json", read=True)
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
        result = run_sources(state, state_path, args.bundle, poll=args.poll_seconds, wait=args.wait_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["pending"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Pending, ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from None
