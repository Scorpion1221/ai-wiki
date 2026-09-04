#!/usr/bin/env python3
"""Deterministically inventory and diff read-only reference repositories.

The scanner unions repositories discovered below a reference root (including explicit
symlink targets), repositories from a cached registry response, and required remotes. It
never writes to the reference root. Remote objects needed for a diff are fetched into a
separate bare cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_MAX_PATHS = 200
DEFAULT_MAX_COMMITS = 50


class ScanError(RuntimeError):
    pass


def run_git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ScanError(detail[-1] if detail else f"git exited {result.returncode}")
    return result.stdout.strip()


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


def git_top_level(path: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
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
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("metadata"), dict):
        return unwrap_checkpoint(value["metadata"])
    for key in ("ai_wiki_incremental_checkpoint_v4", "ai_wiki_incremental_checkpoint_v3"):
        nested = value.get(key)
        if isinstance(nested, str):
            return json.loads(nested)
        if isinstance(nested, dict):
            return nested
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


def remote_branch_head(remote: str, preferred: str | None = None) -> tuple[str, str]:
    branches = list(dict.fromkeys([branch for branch in (preferred, "main", "master") if branch]))
    output = run_git("ls-remote", remote, *(f"refs/heads/{branch}" for branch in branches))
    refs: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2:
            refs[fields[1]] = fields[0]
    for branch in branches:
        sha = refs.get(f"refs/heads/{branch}")
        if sha:
            return branch, sha
    raise ScanError("remote has neither main nor master")


def local_branch_head(path: Path, preferred: str | None = None) -> tuple[str, str]:
    for branch in dict.fromkeys([branch for branch in (preferred, "main", "master") if branch]):
        for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
            sha = run_git("rev-parse", "--verify", ref, cwd=path, check=False)
            if sha:
                return branch, sha
    raise ScanError("local repository has neither main nor master")


def has_commit(git_dir: Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(git_dir), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def prepare_object_repo(
    *, local_path: Path | None, remote: str, branch: str, current: str, previous: str | None, cache: Path
) -> Path:
    if local_path and has_commit(local_path, current) and (not previous or has_commit(local_path, previous)):
        return local_path
    cache.mkdir(parents=True, exist_ok=True)
    if not (cache / "HEAD").exists():
        run_git("init", "--bare", str(cache))
    run_git("fetch", "--quiet", "--no-tags", "--force", remote, f"+refs/heads/{branch}:refs/heads/{branch}", cwd=cache)
    if previous and not has_commit(cache, previous):
        run_git("fetch", "--quiet", "--no-tags", remote, previous, cwd=cache)
    if not has_commit(cache, current) or (previous and not has_commit(cache, previous)):
        raise ScanError("required commit objects are unavailable")
    return cache


def changed_paths(object_repo: Path, previous: str | None, current: str) -> list[dict[str, str]]:
    if previous:
        output = run_git("diff", "--name-status", "--find-renames", previous, current, cwd=object_repo)
        changes: list[dict[str, str]] = []
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            row = {"status": fields[0], "path": fields[-1]}
            if len(fields) == 3:
                row["old_path"] = fields[1]
            changes.append(row)
        return changes
    output = run_git("ls-tree", "-r", "--name-only", current, cwd=object_repo)
    return [{"status": "A", "path": line} for line in output.splitlines() if line]


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


def prefix_counts(changes: list[dict[str, str]], prefixes: list[str]) -> tuple[dict[str, int], list[dict[str, str]]]:
    normalized = [(prefix.strip("/") + "/", prefix.strip("/")) for prefix in prefixes if prefix.strip("/")]
    counts: Counter[str] = Counter()
    priority: list[dict[str, str]] = []
    for change in changes:
        path = change["path"]
        for prefix, label in normalized:
            if path == label or path.startswith(prefix):
                counts[label] += 1
                priority.append(change)
                break
    return dict(sorted(counts.items())), priority


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
    args = parser.parse_args(argv)
    if args.quiet and not args.output:
        parser.error("--quiet requires --output")

    root = args.root.resolve()
    cache_root = args.cache_dir.resolve()
    if cache_root == root or root in cache_root.parents:
        parser.error("--cache-dir must be outside --root")

    local_paths, symlinks = discover_repositories(root)
    checkpoint_by_remote, old_checkpoint = checkpoint_repositories(load_json(args.checkpoint_json))
    registry = registry_urls(load_json(args.registered_json))

    branch_overrides: dict[str, str] = {}
    for item in args.branch_override:
        remote, separator, branch = item.rpartition("=")
        if not separator or not remote or not branch or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
            parser.error(f"invalid --branch-override: {item!r}; expected REMOTE=BRANCH")
        branch_overrides[canonical_remote(remote)] = branch

    inventory: dict[str, dict[str, Any]] = {}

    def add_remote(url: str, source: str, local_path: Path | None = None) -> None:
        try:
            identity = canonical_remote(url)
        except ValueError as exc:
            identity = f"invalid:{hashlib.sha256(url.encode()).hexdigest()[:16]}"
            row = inventory.setdefault(identity, {"fetch_remote": url, "sources": set(), "local_paths": []})
            row["identity_error"] = str(exc)
        else:
            row = inventory.setdefault(identity, {"fetch_remote": url, "sources": set(), "local_paths": []})
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

    results: list[dict[str, Any]] = []
    checkpoint_rows: dict[str, dict[str, Any]] = {}
    for identity in sorted(inventory):
        source = inventory[identity]
        rid = repo_id(identity)
        previous_row = checkpoint_by_remote.get(identity, {})
        previous = previous_row.get("sha")
        preferred_branch = branch_overrides.get(identity) or previous_row.get("branch")
        local_path = Path(source["local_paths"][0]) if source["local_paths"] else None
        output_remote = display_remote(source["fetch_remote"])
        row: dict[str, Any] = {
            "repo_id": rid,
            "name": identity.rsplit("/", 1)[-1],
            "remote_url": output_remote,
            "sources": sorted(source["sources"]),
            "local_paths": sorted(source["local_paths"]),
            "previous_sha": previous,
        }
        try:
            if source.get("identity_error"):
                raise ScanError(source["identity_error"])
            if args.offline:
                if not local_path:
                    raise ScanError("registered/required repository has no local checkout in offline mode")
                branch, current = local_branch_head(local_path, preferred_branch)
            else:
                branch, current = remote_branch_head(source["fetch_remote"], preferred_branch)
            row.update({"branch": branch, "current_sha": current})
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
                        "priority_counts": {},
                        "priority_changes": [],
                        "priority_truncated": False,
                    }
                )
            else:
                object_repo = prepare_object_repo(
                    local_path=local_path,
                    remote=source["fetch_remote"],
                    branch=branch,
                    current=current,
                    previous=previous,
                    cache=cache_root / f"{rid}.git",
                )
                changes = changed_paths(object_repo, previous, current)
                commits, commit_count = commit_rows(object_repo, previous, current, args.max_commits)
                counts, priority = prefix_counts(changes, args.priority_prefix)
                row.update(
                    {
                        "state": "changed" if previous else "new",
                        "baseline_required": previous is None,
                        "object_repo": str(object_repo),
                        "change_count": len(changes),
                        "changes": changes[: args.max_paths],
                        "paths_truncated": len(changes) > args.max_paths,
                        "commit_count": commit_count,
                        "commits": commits,
                        "commits_truncated": commit_count > len(commits),
                        "priority_counts": counts,
                        "priority_changes": priority[: args.max_paths],
                        "priority_truncated": len(priority) > args.max_paths,
                    }
                )
            checkpoint_rows[rid] = {
                "name": row["name"],
                "remote_url": output_remote,
                "branch": branch,
                "sha": current,
            }
        except (OSError, ScanError, ValueError) as exc:
            row.update({"state": "failed", "error": str(exc)})
        results.append(row)

    counts = Counter(row["state"] for row in results)
    registered_identities = {canonical_remote(url) for url in registry}
    required_identities = {canonical_remote(url) for url in args.required_remote}
    scanned_identities = {
        identity for identity, row in zip(sorted(inventory), results, strict=True) if row["state"] != "failed"
    }
    report = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "repo_root": str(root),
        "counts": {
            "registered": len(registered_identities),
            "discovered": len(local_paths),
            "required": len(required_identities),
            "unique": len(results),
            "scanned": len(scanned_identities),
            "changed": counts["changed"],
            "new": counts["new"],
            "unchanged": counts["unchanged"],
            "failed": counts["failed"],
            "registered_missing": len(registered_identities - scanned_identities),
            "required_missing": len(required_identities - scanned_identities),
        },
        "symlinks": symlinks,
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
    incomplete = counts["failed"] or report["counts"]["registered_missing"] or report["counts"]["required_missing"]
    return 2 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
