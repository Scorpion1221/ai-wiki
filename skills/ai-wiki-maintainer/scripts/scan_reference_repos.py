#!/usr/bin/env python3
"""Deterministically inventory and diff read-only reference repositories.

The scanner unions repositories discovered below a reference root (including explicit
symlink targets), repositories from a cached registry response, and required remotes. It
never writes to the reference root. Remote objects needed for a diff are fetched into a
separate bare cache.

Exit codes: 0 every repository scanned; 3 partial (per-repository failures, or checkpoint
repositories missing from this run's inventory; their previous checkpoint rows are carried
forward with ``stale_since``, so ``checkpoint_candidate`` is safe to checkpoint); 2 usage or
fatal error, including a missing ``--root`` or an unusable ``--checkpoint-json`` (no report).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_MAX_PATHS = 200
DEFAULT_MAX_COMMITS = 50
DEFAULT_GIT_TIMEOUT_S = 120
EXIT_OK, EXIT_FATAL, EXIT_PARTIAL = 0, 2, 3
UNLISTED_ERROR = "not in inventory (not discovered/registered/required)"

# Every git call is bounded and can never block on a credential prompt.
GIT_TIMEOUT_S = DEFAULT_GIT_TIMEOUT_S
GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": shutil.which("true") or "/bin/true",
}


class ScanError(RuntimeError):
    pass


def redact(text: str) -> str:
    return re.sub(r"(://)[^/@\s]+@", r"\1***@", text)


def git_process(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env=GIT_ENV,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScanError(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from exc


def run_git(*args: str, cwd: Path | None = None, check: bool = True, strip: bool = True) -> str:
    result = git_process(*args, cwd=cwd)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ScanError(redact(detail[-1]) if detail else f"git {args[0]} exited {result.returncode}")
    return result.stdout.strip() if strip else result.stdout


def canonical_remote(url: str) -> str:
    """Return a credential-free identity shared by HTTPS and scp-style SSH URLs."""
    value = url.strip()
    scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", value)
    if scp and "://" not in value:
        host, path = scp.groups()
    else:
        parsed = urlsplit(value)
        if parsed.scheme == "file":
            return f"file://{Path(parsed.path).resolve().as_posix().rstrip('/')}".removesuffix(".git")
        if not parsed.scheme and Path(value).exists():
            return f"file://{Path(value).resolve().as_posix().rstrip('/')}".removesuffix(".git")
        host = (parsed.hostname or "").lower()
        path = parsed.path
    if not host or not path:
        raise ValueError("remote URL has no host/path")
    return f"{host}/{path.lstrip('/').rstrip('/')}".removesuffix(".git")


def display_remote(url: str) -> str:
    """Return a fetchable-looking URL without embedded credentials."""
    value = url.strip()
    scp = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", value)
    if scp and "://" not in value:
        host, path = scp.groups()
        return f"https://{host.lower()}/{path.lstrip('/')}"
    parsed = urlsplit(value)
    if parsed.scheme == "file":
        return value
    if not parsed.scheme and Path(value).exists():
        return str(Path(value).resolve())
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def repo_id(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def remote_identity(url: str) -> tuple[str, str | None]:
    """Return (identity, error); an unusable URL gets a stable ``invalid:`` identity."""
    try:
        return canonical_remote(url), None
    except ValueError as exc:
        return f"invalid:{hashlib.sha256(url.encode()).hexdigest()[:16]}", str(exc)


def git_top_level(path: Path) -> Path | None:
    result = git_process("rev-parse", "--show-toplevel", cwd=path)
    if result.returncode:
        return None
    return Path(result.stdout.strip()).resolve()


def discover_repositories(root: Path) -> tuple[list[Path], list[dict[str, str]]]:
    """Discover repositories without relying on find(1)'s symlink semantics."""
    repos: set[Path] = set()
    symlinks: list[dict[str, str]] = []
    queue: deque[Path] = deque([root])
    visited: set[Path] = set()
    while queue:
        current = queue.popleft()
        try:
            resolved = current.resolve()
        except OSError:
            continue
        if resolved in visited or not resolved.is_dir():
            continue
        visited.add(resolved)
        top = git_top_level(resolved) if (resolved / ".git").exists() else None
        if top:
            repos.add(top)
            continue
        try:
            entries = sorted(os.scandir(resolved), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name == ".git":
                continue
            path = Path(entry.path)
            if entry.is_symlink():
                target = path.resolve()
                symlinks.append({"path": str(path), "target": str(target)})
                if target.is_dir():
                    queue.append(target)
            elif entry.is_dir(follow_symlinks=False):
                queue.append(path)
    return sorted(repos), symlinks


def load_json(path: Path | None) -> Any:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def registry_urls(value: Any) -> list[str]:
    if value is None:
        return []
    rows = value if isinstance(value, list) else value.get("repositories", value.get("repos", []))
    urls: list[str] = []
    for row in rows:
        if isinstance(row, str):
            urls.append(row)
        elif isinstance(row, dict) and isinstance(row.get("url"), str):
            urls.append(row["url"])
    return urls


def unwrap_checkpoint(value: Any) -> dict[str, Any]:
    """Accept a checkpoint, ``checkpoint.py find`` output, or issue metadata (JSON-string forms too)."""
    for _ in range(2):  # metadata values are JSON strings; ``metadata get`` encodes once more
        if isinstance(value, str):
            value = json.loads(value)
    if not isinstance(value, dict):
        return {}
    for key in ("checkpoint", "metadata", "ai_wiki_incremental_checkpoint_v4", "ai_wiki_incremental_checkpoint_v3"):
        if isinstance(value.get(key), (dict, str)):
            return unwrap_checkpoint(value[key])
    return value


def checkpoint_repositories(value: Any) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    checkpoint = unwrap_checkpoint(value)
    by_identity: dict[str, dict[str, Any]] = {}
    repos = checkpoint.get("repos", {}) if isinstance(checkpoint, dict) else {}
    values = repos.values() if isinstance(repos, dict) else repos
    for row in values:
        if not isinstance(row, dict) or not row.get("remote_url"):
            continue
        try:
            by_identity[canonical_remote(str(row["remote_url"]))] = row
        except ValueError:
            continue
    return by_identity, checkpoint


def local_remote(path: Path) -> str:
    return run_git("config", "--get", "remote.origin.url", cwd=path)


def remote_branch_head(
    remote: str, checkpoint_branch: str | None = None, *, override_branch: str | None = None
) -> tuple[str, str, str, str | None]:
    """Return (branch, sha, selection, remote_default).

    Continuity wins over the advertised default: the checkpoint branch is kept while it
    still exists, so a default-branch change on the remote never silently switches the
    tracked (usually release) branch. An explicit override wins over both.
    """
    def resolve(branch: str) -> str:
        output = run_git("ls-remote", remote, f"refs/heads/{branch}")
        for line in output.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == f"refs/heads/{branch}":
                return fields[0]
        return ""

    output = run_git("ls-remote", "--symref", remote, "HEAD")
    default_branch: str | None = None
    head_sha: str | None = None
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == "ref:" and fields[2] == "HEAD" and fields[1].startswith("refs/heads/"):
            default_branch = fields[1].removeprefix("refs/heads/")
        elif len(fields) == 2 and fields[1] == "HEAD":
            head_sha = fields[0]

    if override_branch:
        sha = resolve(override_branch)
        if sha:
            return override_branch, sha, "override", default_branch
        raise ScanError(f"explicit branch {override_branch!r} does not exist on remote")
    if checkpoint_branch:
        sha = head_sha if checkpoint_branch == default_branch and head_sha else resolve(checkpoint_branch)
        if sha:
            return checkpoint_branch, sha, "checkpoint_continuity", default_branch
    if default_branch and head_sha:
        return default_branch, head_sha, "remote_default", default_branch
    if default_branch:
        sha = resolve(default_branch)
        if sha:
            return default_branch, sha, "remote_default", default_branch

    # A bare cache may have an unset HEAD. Its sole branch is still unambiguous;
    # with a SHA-only HEAD, accept only a uniquely matching branch tip.
    branches = []
    for line in run_git("ls-remote", "--heads", remote).splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].startswith("refs/heads/"):
            branches.append((fields[1].removeprefix("refs/heads/"), fields[0]))
    if head_sha:
        matches = [(branch, sha) for branch, sha in branches if sha == head_sha]
        if len(matches) == 1:
            branch, sha = matches[0]
            return branch, sha, "remote_head_sha", branch
        raise ScanError("remote HEAD SHA has no unique matching branch; use --branch-override")
    if len(branches) == 1:
        branch, sha = branches[0]
        return branch, sha, "sole_branch", default_branch
    raise ScanError("remote default branch unavailable or ambiguous; use --branch-override")


def local_branch_head(
    path: Path, checkpoint_branch: str | None = None, *, override_branch: str | None = None
) -> tuple[str, str, str, str | None]:
    """Offline twin of remote_branch_head; origin/HEAD may be stale."""
    def resolve(branch: str) -> str:
        for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
            sha = run_git("rev-parse", "--verify", ref, cwd=path, check=False)
            if sha:
                return sha
        return ""

    remote_head = run_git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", cwd=path, check=False)
    default_branch = (
        remote_head.removeprefix("refs/remotes/origin/") if remote_head.startswith("refs/remotes/origin/") else None
    )
    if override_branch:
        sha = resolve(override_branch)
        if sha:
            return override_branch, sha, "override", default_branch
        raise ScanError(f"explicit branch {override_branch!r} does not exist locally")
    if checkpoint_branch:
        sha = resolve(checkpoint_branch)
        if sha:
            return checkpoint_branch, sha, "checkpoint_continuity", default_branch
    if default_branch:
        sha = resolve(default_branch)
        if sha:
            return default_branch, sha, "local_origin_head", default_branch

    for prefix in ("refs/remotes/origin", "refs/heads"):
        output = run_git("for-each-ref", "--format=%(refname) %(objectname)", prefix, cwd=path)
        branches = []
        for line in output.splitlines():
            ref, _, sha = line.partition(" ")
            if ref.startswith(f"{prefix}/") and ref != "refs/remotes/origin/HEAD" and sha:
                branches.append((ref.removeprefix(f"{prefix}/"), sha))
        if len(branches) == 1:
            branch, sha = branches[0]
            return branch, sha, "sole_branch", default_branch
        if branches:
            break
    raise ScanError("local default branch unavailable or ambiguous; use --branch-override")


def has_commit(git_dir: Path, sha: str) -> bool:
    return git_process("cat-file", "-e", f"{sha}^{{commit}}", cwd=git_dir).returncode == 0


def is_ancestor(git_dir: Path, ancestor: str, descendant: str) -> bool:
    result = git_process("merge-base", "--is-ancestor", ancestor, descendant, cwd=git_dir)
    if result.returncode not in (0, 1):
        raise ScanError(redact(result.stderr.strip()) or "git merge-base --is-ancestor failed")
    return result.returncode == 0


def merge_base(git_dir: Path, first: str, second: str) -> str | None:
    result = git_process("merge-base", first, second, cwd=git_dir)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise ScanError(redact(result.stderr.strip()) or "git merge-base failed")
    return result.stdout.strip() or None


def prepare_object_repo(
    *, local_path: Path | None, remote: str, branch: str, current: str,
    previous: str | None, previous_branch: str | None, cache: Path
) -> tuple[Path, bool]:
    """Return (object repo, whether ``previous`` is available there).

    Only the current commit is required; a missing previous commit means the tracked
    history was rewritten or the old branch is gone, which the caller rebaselines.
    """
    if local_path and has_commit(local_path, current) and (not previous or has_commit(local_path, previous)):
        return local_path, bool(previous)
    cache.mkdir(parents=True, exist_ok=True)
    if not (cache / "HEAD").exists():
        run_git("init", "--bare", str(cache))
    run_git("fetch", "--quiet", "--no-tags", "--force", remote, f"+refs/heads/{branch}:refs/heads/{branch}", cwd=cache)
    if previous and not has_commit(cache, previous):
        if previous_branch and previous_branch != branch:
            run_git(
                "fetch", "--quiet", "--no-tags", "--force", remote,
                f"+refs/heads/{previous_branch}:refs/heads/{previous_branch}", cwd=cache, check=False,
            )
        if not has_commit(cache, previous):
            run_git("fetch", "--quiet", "--no-tags", remote, previous, cwd=cache, check=False)
    if not has_commit(cache, current):
        raise ScanError("required commit objects are unavailable")
    return cache, bool(previous) and has_commit(cache, previous)


def changed_paths(object_repo: Path, previous: str | None, current: str) -> list[dict[str, str]]:
    # -z keeps paths verbatim; the default core.quotePath would quote and octal-escape
    # non-ASCII names such as tasks/<中文 task root>.
    if previous:
        fields = run_git(
            "diff", "--name-status", "-z", "--find-renames", previous, current, cwd=object_repo, strip=False
        ).split("\0")
        changes: list[dict[str, str]] = []
        index = 0
        while index + 1 < len(fields) and fields[index]:
            status = fields[index]
            if status[:1] in ("R", "C"):
                changes.append({"status": status, "old_path": fields[index + 1], "path": fields[index + 2]})
                index += 3
            else:
                changes.append({"status": status, "path": fields[index + 1]})
                index += 2
        return changes
    output = run_git("ls-tree", "-r", "-z", "--name-only", current, cwd=object_repo, strip=False)
    return [{"status": "A", "path": path} for path in output.split("\0") if path]


def commit_rows(object_repo: Path, previous: str | None, current: str, limit: int) -> tuple[list[dict[str, str]], int]:
    revision = f"{previous}..{current}" if previous else current
    count = int(run_git("rev-list", "--count", revision, cwd=object_repo) or "0")
    output = run_git(
        "log",
        f"--max-count={limit}",
        "--format=%H%x1f%cI%x1f%s",
        revision,
        cwd=object_repo,
    )
    rows = []
    for line in output.splitlines():
        fields = line.split("\x1f", 2)
        if len(fields) == 3:
            rows.append({"sha": fields[0], "committed_at": fields[1], "subject": fields[2]})
    return rows, count


def path_groups(changes: list[dict[str, str]]) -> dict[str, int]:
    """Count every changed path by top-level directory ("." for root files), untruncated."""
    counts = Counter(path.split("/", 1)[0] if "/" in path else "." for path in (row["path"] for row in changes))
    return dict(sorted(counts.items()))


def prefix_counts(
    changes: list[dict[str, str]], prefixes: list[str]
) -> tuple[dict[str, int], list[dict[str, str]], dict[str, int]]:
    """Return per-prefix counts, matching changes, and counts per entry below each prefix.

    The last value groups ``tasks/<task-root>/...`` style paths by their first component
    under the prefix, so a context repository's changed task roots stay visible even when
    the path list itself is truncated.
    """
    normalized = [(prefix.strip("/") + "/", prefix.strip("/")) for prefix in prefixes if prefix.strip("/")]
    counts: Counter[str] = Counter()
    groups: Counter[str] = Counter()
    priority: list[dict[str, str]] = []
    for change in changes:
        path = change["path"]
        for prefix, label in normalized:
            if path == label or path.startswith(prefix):
                counts[label] += 1
                priority.append(change)
                rest = path[len(prefix):] if path != label else ""
                groups[f"{label}/{rest.split('/', 1)[0]}" if rest else label] += 1
                break
    return dict(sorted(counts.items())), priority, dict(sorted(groups.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="reference repository root")
    parser.add_argument("--registered-json", type=Path, help="cached registry response")
    parser.add_argument("--checkpoint-json", type=Path, help="v3/v4 checkpoint or issue metadata JSON")
    parser.add_argument("--required-remote", action="append", default=[], help="remote that must be scanned")
    parser.add_argument(
        "--branch-override",
        action="append",
        default=[],
        metavar="REMOTE=BRANCH",
        help="pin a repository to an explicit branch (repeatable)",
    )
    parser.add_argument("--priority-prefix", action="append", default=[], help="path prefix highlighted in output")
    parser.add_argument("--cache-dir", type=Path, required=True, help="bare object cache outside reference root")
    parser.add_argument("--output", type=Path, help="write the JSON report here as well as stdout")
    parser.add_argument("--quiet", action="store_true", help="write only --output; suppress JSON on stdout")
    parser.add_argument("--offline", action="store_true", help="use local refs; registered-only repos will fail")
    parser.add_argument("--max-paths", type=int, default=DEFAULT_MAX_PATHS)
    parser.add_argument("--max-commits", type=int, default=DEFAULT_MAX_COMMITS)
    parser.add_argument(
        "--git-timeout", type=int, default=DEFAULT_GIT_TIMEOUT_S, help="seconds allowed for each git call"
    )
    args = parser.parse_args(argv)
    if args.quiet and not args.output:
        parser.error("--quiet requires --output")
    if args.git_timeout < 1:
        parser.error("--git-timeout must be positive")
    global GIT_TIMEOUT_S
    GIT_TIMEOUT_S = args.git_timeout
    try:
        return scan(args, parser)
    except (OSError, ValueError, ScanError) as exc:
        print(f"scan_reference_repos: fatal: {redact(str(exc))}", file=sys.stderr)
        return EXIT_FATAL


def scan(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    root = args.root.resolve()
    cache_root = args.cache_dir.resolve()
    if cache_root == root or root in cache_root.parents:
        parser.error("--cache-dir must be outside --root")
    if args.output:
        output = args.output.resolve()
        if output == root or root in output.parents:
            parser.error("--output must be outside --root")

    if not root.is_dir():
        # An unmounted root would otherwise look like an empty inventory.
        raise ScanError(f"--root {root} is not a directory")
    local_paths, symlinks = discover_repositories(root)
    for option, path in (("--cache-dir", cache_root), ("--output", output if args.output else None)):
        if path is not None and any(path == repo or repo in path.parents for repo in local_paths):
            parser.error(f"{option} must be outside discovered reference repositories")
    checkpoint_by_remote, old_checkpoint = checkpoint_repositories(load_json(args.checkpoint_json))
    if args.checkpoint_json and not checkpoint_by_remote:
        # Scanning as if there were no checkpoint would rebaseline every repo and drop the rest.
        raise ScanError(f"--checkpoint-json {args.checkpoint_json} has no usable repos rows")
    registry = registry_urls(load_json(args.registered_json))

    branch_overrides: dict[str, str] = {}
    for item in args.branch_override:
        remote, separator, branch = item.rpartition("=")
        if not separator or not remote or not branch or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
            parser.error(f"invalid --branch-override: {item!r}; expected REMOTE=BRANCH")
        try:
            branch_overrides[canonical_remote(remote)] = branch
        except ValueError:
            parser.error(f"invalid --branch-override remote: {item!r}")

    inventory: dict[str, dict[str, Any]] = {}

    def add_remote(url: str, source: str, local_path: Path | None = None) -> None:
        identity, error = remote_identity(url)
        row = inventory.setdefault(identity, {"fetch_remote": url, "sources": set(), "local_paths": []})
        if error:
            row["identity_error"] = error
        row["sources"].add(source)
        if local_path and str(local_path) not in row["local_paths"]:
            row["local_paths"].append(str(local_path))

    for path in local_paths:
        try:
            add_remote(local_remote(path), "reference_root", path)
        except ScanError:
            add_remote(str(path), "reference_root", path)
    for url in registry:
        add_remote(url, "registered")
    for url in args.required_remote:
        add_remote(url, "required")

    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    warnings: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    checkpoint_rows: dict[str, dict[str, Any]] = {}
    for identity in sorted(inventory):
        source = inventory[identity]
        rid = repo_id(identity)
        previous_row = checkpoint_by_remote.get(identity, {})
        previous = previous_row.get("sha")
        previous_branch = previous_row.get("branch")
        override_branch = branch_overrides.get(identity)
        local_path = Path(source["local_paths"][0]) if source["local_paths"] else None
        try:
            output_remote = display_remote(source["fetch_remote"])
        except ValueError:
            output_remote = redact(source["fetch_remote"])
        row: dict[str, Any] = {
            "repo_id": rid,
            "name": identity.rsplit("/", 1)[-1],
            "remote_url": output_remote,
            "sources": sorted(source["sources"]),
            "local_paths": sorted(source["local_paths"]),
            "previous_sha": previous,
            "previous_branch": previous_branch,
        }
        try:
            if source.get("identity_error"):
                raise ScanError(source["identity_error"])
            if args.offline:
                if not local_path:
                    raise ScanError("registered/required repository has no local checkout in offline mode")
                branch, current, selection, remote_default = local_branch_head(
                    local_path, previous_branch, override_branch=override_branch
                )
            else:
                branch, current, selection, remote_default = remote_branch_head(
                    source["fetch_remote"], previous_branch, override_branch=override_branch
                )
            rebaseline_reason = None
            if previous and previous_branch and branch != previous_branch:
                rebaseline_reason = "branch_override" if selection == "override" else "checkpoint_branch_missing"
            row.update({
                "branch": branch,
                "current_sha": current,
                "branch_selection": selection,
                "remote_default": remote_default,
                "branch_changed": rebaseline_reason is not None,
            })
            if selection == "checkpoint_continuity" and remote_default and remote_default != branch:
                warnings.append({
                    "type": "default_branch_drift",
                    "repo": row["name"],
                    "remote_url": output_remote,
                    "checkpoint_branch": branch,
                    "remote_default": remote_default,
                })
            if current == previous:
                row.update(
                    {
                        "state": "unchanged",
                        "change_count": 0,
                        "changes": [],
                        "paths_truncated": False,
                        "commit_count": 0,
                        "commits": [],
                        "commits_truncated": False,
                        "path_groups": {},
                        "priority_counts": {},
                        "priority_groups": {},
                        "priority_changes": [],
                        "priority_truncated": False,
                    }
                )
            else:
                object_repo, has_previous = prepare_object_repo(
                    local_path=local_path,
                    remote=source["fetch_remote"],
                    branch=branch,
                    current=current,
                    previous=previous,
                    previous_branch=previous_branch,
                    cache=cache_root / f"{rid}.git",
                )
                base = previous
                if previous and not rebaseline_reason and not (
                    has_previous and is_ancestor(object_repo, previous, current)
                ):
                    rebaseline_reason = "history_rewritten"
                if rebaseline_reason:
                    # Never diff across lineages: only what the tracked branch added since
                    # the fork point, or its whole tree when no common history is available.
                    base = merge_base(object_repo, previous, current) if has_previous else None
                    row.update({"rebaseline_reason": rebaseline_reason, "merge_base": base})
                    state = "rebaselined"
                else:
                    state = "changed" if previous else "new"
                changes = changed_paths(object_repo, base, current)
                commits, commit_count = commit_rows(object_repo, base, current, args.max_commits)
                priority_counts, priority, priority_groups = prefix_counts(changes, args.priority_prefix)
                row.update(
                    {
                        "state": state,
                        "baseline_required": state != "changed",
                        "object_repo": str(object_repo),
                        "change_count": len(changes),
                        "changes": changes[: args.max_paths],
                        "paths_truncated": len(changes) > args.max_paths,
                        "path_groups": path_groups(changes),
                        "commit_count": commit_count,
                        "commits": commits,
                        "commits_truncated": commit_count > len(commits),
                        "priority_counts": priority_counts,
                        "priority_groups": priority_groups,
                        "priority_changes": priority[: args.max_paths],
                        "priority_truncated": len(priority) > args.max_paths,
                    }
                )
                truncated = [
                    field for field, flag in (
                        ("changes", row["paths_truncated"]),
                        ("commits", row["commits_truncated"]),
                        ("priority_changes", row["priority_truncated"]),
                    ) if flag
                ]
                if truncated:
                    # Loud on purpose: a truncated list is not coverage. path_groups and
                    # priority_groups are complete; read the object repo for the rest.
                    warnings.append({
                        "type": "truncated",
                        "repo": row["name"],
                        "remote_url": output_remote,
                        "fields": truncated,
                        "change_count": len(changes),
                        "commit_count": commit_count,
                        "priority_count": len(priority),
                        "max_paths": args.max_paths,
                        "max_commits": args.max_commits,
                    })
            checkpoint_rows[rid] = {
                "name": row["name"],
                "remote_url": output_remote,
                "branch": branch,
                "sha": current,
            }
        except (OSError, ScanError, ValueError) as exc:
            error = redact(str(exc))
            row.update({"state": "failed", "error": error})
            if previous_row.get("sha"):
                # Keep the last good row so a partial checkpoint never resets this repo to "new".
                carried = {key: previous_row[key] for key in ("name", "remote_url", "branch", "sha")
                           if key in previous_row}
                carried.setdefault("name", row["name"])
                carried["stale_since"] = previous_row.get("stale_since") or generated_at
                carried["last_error"] = error[:500]
                checkpoint_rows[rid] = carried
                row["stale_since"] = carried["stale_since"]
        results.append(row)

    unlisted = sorted(set(checkpoint_by_remote) - set(inventory))
    for identity in unlisted:
        # Not discovered, registered, or required this run (e.g. a missing symlink). Keep the
        # row rather than dropping its history, and age it like a failed repo: it was not scanned.
        previous_row = dict(checkpoint_by_remote[identity])
        previous_row.setdefault("name", identity.rsplit("/", 1)[-1])
        previous_row["stale_since"] = previous_row.get("stale_since") or generated_at
        previous_row["last_error"] = UNLISTED_ERROR
        checkpoint_rows[repo_id(identity)] = previous_row
        warnings.append({
            "type": "checkpoint_repo_unlisted",
            "repo": previous_row["name"],
            "remote_url": previous_row["remote_url"],
        })

    counts = Counter(row["state"] for row in results)
    registered_identities = {remote_identity(url)[0] for url in registry}
    required_identities = {remote_identity(url)[0] for url in args.required_remote}
    scanned_identities = {
        identity for identity, row in zip(sorted(inventory), results, strict=True) if row["state"] != "failed"
    }
    report = {
        "version": 1,
        "generated_at": generated_at,
        "repo_root": str(root),
        "counts": {
            "registered": len(registered_identities),
            "discovered": len(local_paths),
            "required": len(required_identities),
            "unique": len(results),
            "scanned": len(scanned_identities),
            "changed": counts["changed"],
            "new": counts["new"],
            "rebaselined": counts["rebaselined"],
            "unchanged": counts["unchanged"],
            "failed": counts["failed"],
            "carried_forward": sum(1 for row in results if row["state"] == "failed" and row.get("stale_since")),
            "truncated": sum(1 for warning in warnings if warning["type"] == "truncated"),
            "unlisted": len(unlisted),
            "registered_missing": len(registered_identities - scanned_identities),
            "required_missing": len(required_identities - scanned_identities),
        },
        "symlinks": symlinks,
        "warnings": warnings,
        "repos": results,
        "checkpoint_candidate": {
            "version": 4,
            "repo_root": str(root),
            "repos": checkpoint_rows,
            "issues": old_checkpoint.get("issues"),
        },
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if not args.quiet:
        sys.stdout.write(rendered)
    incomplete = (
        counts["failed"] or unlisted or report["counts"]["registered_missing"] or report["counts"]["required_missing"]
    )
    code = EXIT_PARTIAL if incomplete else EXIT_OK
    summary = " ".join(f"{key}={value}" for key, value in report["counts"].items() if value)
    print(f"scan_reference_repos: exit={code} {summary}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
