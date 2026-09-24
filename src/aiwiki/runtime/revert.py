"""Admin revert of committed changesets (design §8.5): one deterministic commit on the writer.

No agent runs and nothing is merged as text. Newest first, every file a changeset commit
wrote goes back to the bytes it had before that commit, but only while it still holds what
the commit left: a file-level compare-and-swap, as in the gate's G6. A concept compares by
its content hash, so an audit's stamp on the changeset's content does not block the revert
(the stamp goes with the content); any other file compares by bytes. The first changeset with
a file changed since then stops the revert there; the newer ones are still reverted.
Service-owned files (indexes, log.md, the source hash ledger, viz.html) are rebuilt by the
closeout instead, and log.md gains a Revert entry, so the append-only ledger keeps its
history. The result may add no high lint finding, and no validation error outside the
restored files; the first changeset whose undo would add one stops the revert there too. It
is committed with trailers and pushed under the writer lock, and a push-time rebase re-runs
the compare-and-swap and that check on the merged tree (G14).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..engine import scan_sources
from ..engine.lint import lint
from ..engine.validate import should_check
from ..engine.validate import validate as validate_bundle
from ..version import service_identity
from . import changeset, curate, secrets
from .failure import failure, phase_stage

# Rebuilt by the closeout, never reverted: a textual revert of these conflicts with every
# later commit, and would erase log.md history.
_REBUILT = frozenset({"log.md", "viz.html", "sources/.hashes.yaml"})


def _rebuilt(rel: str) -> bool:
    return rel in _REBUILT or rel.rsplit("/", 1)[-1] == "index.md"


def _blob(root: Path, revision: str, rel: str) -> bytes | None:
    """The bytes of ``rel`` at ``revision``, binary safe; None where it does not exist."""
    shown = subprocess.run(["git", "-C", str(root), "cat-file", "blob", f"{revision}:{rel}"],
                           capture_output=True, timeout=curate.GIT_TIMEOUT_S)
    return shown.stdout if shown.returncode == 0 else None


def _current(bundle: Path, rel: str) -> bytes | None:
    path = bundle / rel
    return path.read_bytes() if path.is_file() and not path.is_symlink() else None


def _put(bundle: Path, rel: str, data: bytes | None) -> None:
    path = bundle / rel
    if data is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _unchanged(bundle: Path, rel: str, current: bytes | None, left: bytes | None) -> bool:
    """Whether ``rel`` still holds what the commit left: by content hash for a concept (§2.6)."""
    if current == left:
        return True
    if current is None or left is None or not should_check(bundle / rel, bundle):
        return False
    return changeset.content_hash(current.decode("utf-8", "replace")) == changeset.content_hash(
        left.decode("utf-8", "replace"))


def _undo(root: Path, bundle: Path, commit: str) -> tuple[dict[str, bytes | None], list[dict], list[str]]:
    """What undoes one commit in the working tree: ``{path: bytes before it}`` (None: absent),
    the conflicts that stop it, and the directory indexes it added."""
    listed = curate._git(root, "diff", "--name-only", "--no-renames", "-z", f"{commit}^", commit)
    if listed.returncode != 0:
        raise RuntimeError(f"cannot list the files of commit {commit}")
    writes: dict[str, bytes | None] = {}
    conflicts, indexes = [], []
    for rel in filter(None, listed.stdout.split("\0")):
        if _rebuilt(rel):
            if rel.endswith("index.md") and _blob(root, f"{commit}^", rel) is None:
                indexes.append(rel)
        elif not _unchanged(bundle, rel, _current(bundle, rel), left := _blob(root, commit, rel)):
            # The first later commit that changed what the changeset left is the one it cannot step over.
            later = curate._git(root, "log", "--reverse", "--format=%H", f"{commit}..HEAD", "--", rel).stdout.split()
            changed_by = next((sha for sha in later if not _unchanged(bundle, rel, _blob(root, sha, rel), left)), None)
            conflicts.append(changeset._error("conflict", "the file changed after the changeset", rel,
                                              changed_by=changed_by))
        else:
            writes[rel] = _blob(root, f"{commit}^", rel)
    return writes, conflicts, indexes


def _problems(bundle: Path) -> dict[tuple[str, str], tuple[str, bool]]:
    """Validation errors and high lint findings (a link to a removed concept) by error key,
    each with whether lint found it."""
    findings, _count = lint(bundle)
    found = {changeset.error_key(message): (message, False) for message in validate_bundle(bundle)}
    for message in (f"{f['where']}: {f['detail']}" for f in findings if f["severity"] == "high"):
        found[changeset.error_key(message)] = (message, True)
    return found


def _new(before: dict, bundle: Path, restored) -> list[str]:
    """Problems the revert introduced, that the bundle did not have. A restored file holds
    bytes once published, so its own old validation errors come back with it; a high lint
    finding in it (a link into a concept an older changeset created) still counts, as G12
    counts one in a file a changeset writes."""
    return [message for key, (message, linted) in _problems(bundle).items()
            if key not in before and (linted or key[0] not in restored)]


def _recheck(root: Path, bundle: Path, judged: str, written: dict, before: dict) -> list[dict]:
    """G14 after a push-time rebase: each reverted file is upstream as the revert found it."""
    upstream = f"origin/{curate._branch(root)}"
    errors = [changeset._error("conflict", "the file changed upstream during the revert", rel)
              for rel in sorted(written) if _blob(root, upstream, rel) != _blob(root, judged, rel)]
    return errors + [changeset._from_message(message) for message in _new(before, bundle, written)]


def _message(job: dict) -> str:
    """One summary line, then trailers naming this job and every changeset commit it reverts."""
    commits = {entry["id"]: entry["commit"] for entry in job["changesets"]}
    reverted = job["reverted"]
    what = f"changeset {reverted[0]}" if len(reverted) == 1 else f"{len(reverted)} changesets"
    reason = curate._one_line(job.get("reason"))
    trailers = [f"Revert: {job['id']}", *(f"Reverts-Changeset: {cs} {commits[cs]}" for cs in reverted),
                f"Principal: {job['actor']}"]
    if job.get("run"):
        trailers.append("Run: " + curate._one_line(job["run"]))
    summary = f"revert: {what}" + (f" ({reason})" if reason else "")
    return secrets.redact(summary + "\n\n" + "\n".join(trailers) + "\n")[0]


def run(bundle: Path, job_path: Path) -> None:
    """Run one queued revert job; the serial worker holds the writer lock. Never raises."""
    job = json.loads(job_path.read_text(encoding="utf-8"))
    job.update(status="running", started=curate._now(), service=service_identity())
    curate._save(job_path, job)
    root = curate._repo_root(bundle)
    try:
        _transaction(root, bundle, job, job_path)
    except Exception as exc:  # noqa: BLE001 — record any failure on the job, never crash the worker
        git_timeout = isinstance(exc, subprocess.TimeoutExpired)
        job.update(status="failed", error=repr(exc), failure=failure(
            "transient" if git_timeout else "internal", stage=phase_stage(job), detail=repr(exc)))
        if root is not None and job.get("base_revision"):
            curate._rollback_git(root, job["base_revision"])
            job["phase"] = "rolled_back"
    if job["status"] != "done":
        job["reverted"] = []  # nothing was published, so nothing was reverted
    job["finished"] = curate._now()
    curate._save(job_path, job)


def _settle(root: Path, bundle: Path, job: dict, steps: list, before: dict) -> tuple[dict, list[str]]:
    """The clean base with ``steps`` undone and the closeout run: the restored files, and the
    problems that leaves (G12, in the manner of the gate)."""
    curate._rollback_git(root, job["base_revision"])
    written: dict[str, bytes | None] = {}
    indexes: list[str] = []
    for _entry, writes, added in steps:
        for rel, data in writes.items():
            _put(bundle, rel, data)
        written.update(writes)
        indexes += added
    job["reverted"] = [entry["id"] for entry, _writes, _added in steps]
    # A source snapshot a remaining concept still cites is that concept's evidence: it stays.
    removed = {rel for rel, data in written.items() if data is None and rel.startswith("sources/")}
    job["kept_sources"] = sorted(rel for rel, cited in scan_sources._affected(bundle, removed).items() if cited)
    for rel in job["kept_sources"]:
        del written[rel]
        _put(bundle, rel, _blob(root, "HEAD", rel))
    job["concept_files"] = sorted(rel for rel in written if should_check(bundle / rel, bundle))
    subject = f"Reverted changeset{'s' if len(job['reverted']) > 1 else ''} " + ", ".join(job["reverted"])
    job["closeout"] = curate._deterministic_closeout(bundle, "", job["concept_files"], subject, op="revert")
    for rel in indexes:  # a directory the revert emptied keeps no index
        if not any(should_check(path, bundle) for path in (bundle / rel).parent.rglob("*.md")):
            (bundle / rel).unlink(missing_ok=True)
    return written, _new(before, bundle, written)


def _transaction(root: Path | None, bundle: Path, job: dict, job_path: Path) -> None:
    if root is None or root.resolve() != bundle.resolve():
        detail = "a revert needs a bundle that is its own Git repository"
        job.update(status="failed", error=detail, failure=failure("internal", detail=detail, retryable=False))
        return
    curate._exclude_inbox(root, bundle)
    if curate._working_files(root) or curate._bundle_symlinks(bundle):
        detail = "a revert needs a clean working tree without symlinks"
        job.update(status="failed", error=detail, failure=failure("internal", detail=detail, retryable=False))
        return
    job.update(base_revision=curate._git(root, "rev-parse", "HEAD").stdout.strip(),
               base_branch=curate._branch(root), phase="syncing")
    curate._save(job_path, job)
    job["pre_sync"] = curate._pre_sync(root, strict=True)  # G5: never revert on a stale base
    if job["pre_sync"].get("refused"):
        detail = f"pre-sync refused a stale base: {job['pre_sync']['note']}"
        job.update(status="failed", error=detail, failure=failure("transient", stage="pre_sync", detail=detail))
        return
    job.update(base_revision=curate._git(root, "rev-parse", "HEAD").stdout.strip(), phase="prepared")
    curate._save(job_path, job)

    order = {sha: n for n, sha in enumerate(curate._git(root, "rev-list", "HEAD").stdout.split())}
    missing = [entry["id"] for entry in job["changesets"] if entry["commit"] not in order]
    if missing:
        job.update(changeset._rejected({}, [changeset._error(
            "unknown_base", f"changeset {cs} is not on the published branch", changeset=cs) for cs in missing]))
        return
    drift = curate._source_drift_errors(bundle)
    if drift:  # the closeout re-records sources/.hashes.yaml: never over evidence nobody recorded
        detail = "a revert refuses pre-existing source drift"
        job.update(status="failed", error=detail,
                   validation={"status": "not_run", "reason": "source drift preflight failed", "errors": drift[:20]},
                   failure=failure("input", stage="validation", detail=detail, retryable=False))
        return
    before = _problems(bundle)
    steps: list[tuple[dict, dict[str, bytes | None], list[str]]] = []
    targets = sorted(job["changesets"], key=lambda entry: order[entry["commit"]])  # newest first
    for n, entry in enumerate(targets):
        writes, conflicts, added = _undo(root, bundle, entry["commit"])
        if conflicts:
            job["stopped"] = {"changeset": entry["id"], "commit": entry["commit"], "conflicts": conflicts}
            job["pending"] = [later["id"] for later in targets[n + 1:]]
            break
        for rel, data in writes.items():  # an older changeset's compare-and-swap sees this undo
            _put(bundle, rel, data)
        steps.append((entry, writes, added))
    if not steps:
        job.update(changeset._rejected({}, job["stopped"]["conflicts"]))
        return

    written, problems = _settle(root, bundle, job, steps, before)
    if problems and len(steps) > 1:
        # Newest first, the first changeset whose undo leaves a problem stops the revert there.
        for k in range(1, len(steps)):
            _written, found = _settle(root, bundle, job, steps[:k], before)
            if found:
                problems, steps = found, steps[:k]
                break
        if len(steps) > 1:
            stop, steps = steps[-1][0], steps[:-1]
            job["stopped"] = {"changeset": stop["id"], "commit": stop["commit"],
                              "errors": [changeset._from_message(message) for message in problems]}
            job["pending"] = [entry["id"] for entry in targets[targets.index(stop) + 1:]]
            written, problems = _settle(root, bundle, job, steps, before)
    if problems:
        job.update(changeset._rejected({}, [changeset._from_message(message) for message in problems]))
        curate._rollback_git(root, job["base_revision"])
        job["phase"] = "rolled_back"
        return
    job["validation"] = {"status": "passed", "error_count": 0}
    visualization = curate._refresh_visualization(root, bundle)
    if visualization is not None:
        job["visualization"] = visualization
    job["phase"] = "before_commit"
    curate._save(job_path, job)

    def persist(phase: str, result: dict) -> None:
        job.update(phase=phase, git=result, commit=result.get("commit"), changed_files=result.get("changed_files", []))
        curate._save(job_path, job)

    judged = job["base_revision"]
    result = curate._commit_and_push(root, _message(job), 4, persist, bundle,
                                     lambda: _recheck(root, bundle, judged, written, before))
    problems = result.pop("recheck_errors", None)
    job.update(git=result, commit=result.get("commit"), changed_files=result.get("changed_files", []))
    if result.get("committed") and (result.get("pushed") or not curate._has_remote(root)):
        job.update(status="done", phase="done")
        return
    curate._rollback_git(root, job["base_revision"])
    job["phase"] = "rolled_back"
    if problems or result.get("note") == curate.REBASE_CONFLICT:
        # The branch moved at push time. Whatever the merged tree failed, it is a 409: the
        # same revert runs again from the new head.
        job.update(changeset._rejected({}, problems or [changeset._error(
            "conflict", "the published branch moved and Git could not rebase onto it")]))
        job.update(http_status=409, failure=failure("conflict", stage="git", detail=job["failure"]["detail"]))
    else:
        detail = f"revert git commit/push failed: {result.get('note', '')}"
        job.update(status="failed", error=detail, failure=failure("transient", stage="git", detail=detail))
