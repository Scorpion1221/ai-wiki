#!/usr/bin/env python3
"""Find, build, and write the AI Wiki incremental checkpoint deterministically.

  find   latest valid ``ai_wiki_incremental_checkpoint_v4`` among an autopilot's run issues,
         chosen by the checkpoint's own ``completed_at`` (not issue title or run status);
         falls back to the latest valid v3 when no v4 exists.
  build  merge a scanner ``checkpoint_candidate`` with the issues cursor and ``completed_at``
         into a validated v4. With ``--previous`` it refuses to drop repos, move the cursor
         backwards, skip issues between cursors, or use a scan made against another
         checkpoint. Every ``baseline_required`` row (new or rebaselined) needs
         ``--baseline-done`` or ``--baseline-waive``; the decision is recorded on the row.
  write  set the v4 on an issue, read it back, and compare.

Only the ``multica`` CLI is used. ``find`` is read-only. Exit codes: 0 ok; 1 every run issue
was read and none holds a checkpoint; 2 usage, validation, or Multica failure, including a
``find`` that could not read some run issues and found no v4 (never a silent bootstrap or v3
fallback); 3 ``find`` found a v4 but some run issues could not be read (it may be older than
the newest, never newer).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scan_reference_repos import canonical_remote, repo_id, unwrap_checkpoint

V4_KEY = "ai_wiki_incremental_checkpoint_v4"
V3_KEY = "ai_wiki_incremental_checkpoint_v3"
KEYS = {4: V4_KEY, 3: V3_KEY}
SHA = re.compile(r"[0-9a-f]{40}")
PAGE_SIZE = 100
MAX_PAGES = 200
EXIT_OK, EXIT_NOT_FOUND, EXIT_FATAL, EXIT_PARTIAL = 0, 1, 2, 3
UNREADABLE_METADATA = "unexpected `multica issue metadata list` response"


class CheckpointError(ValueError):
    pass


class MulticaError(RuntimeError):
    pass


def run_multica(args: list[str], *, cache: Path | None = None, timeout: int = 120) -> Any:
    """Run one multica command and return its parsed JSON stdout (None when empty)."""
    label = " ".join(args[:3])
    try:
        result = subprocess.run(
            ["multica", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise MulticaError("multica CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MulticaError(f"multica {label} timed out after {timeout}s") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise MulticaError(f"multica {label} exited {result.returncode}: {detail[-1] if detail else ''}".strip())
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(result.stdout, encoding="utf-8")
    text = result.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MulticaError(f"multica {label} returned non-JSON output") from exc


def parse_time(value: Any, field: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value:
        raise CheckpointError(f"{field} must be an RFC 3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CheckpointError(f"{field} is not RFC 3339: {value!r}") from exc
    if parsed.tzinfo is None:
        raise CheckpointError(f"{field} has no timezone: {value!r}")
    return parsed.astimezone(UTC)


def format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def decode(value: Any) -> Any:
    """Metadata values are stored as JSON strings; ``metadata get`` adds one more encoding."""
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CheckpointError("checkpoint value is not JSON") from exc
    return value


def validate(checkpoint: Any, version: int = 4) -> dict[str, Any]:
    """Return the checkpoint unchanged, or raise CheckpointError listing every problem."""
    if not isinstance(checkpoint, dict):
        raise CheckpointError("checkpoint must be a JSON object")
    errors: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    def check_time(value: Any, field: str) -> None:
        try:
            parse_time(value, field)
        except CheckpointError as exc:
            errors.append(str(exc))

    check(checkpoint.get("version") == version, f"version must be {version}")
    check_time(checkpoint.get("completed_at"), "completed_at")
    if version == 4:
        check(isinstance(checkpoint.get("repo_root"), str) and bool(checkpoint.get("repo_root")),
              "repo_root must be a non-empty string")
    issues = checkpoint.get("issues")
    if isinstance(issues, dict):
        check_time(issues.get("updated_at"), "issues.updated_at")
        check(isinstance(issues.get("id"), str) and bool(issues.get("id")), "issues.id must be a non-empty string")
    else:
        errors.append("issues must be an object {updated_at, id}")
    repos = checkpoint.get("repos")
    if not isinstance(repos, dict) or not repos:
        errors.append("repos must be a non-empty object")
        repos = {}
    for key, row in repos.items():
        where = f"repos[{key}]"
        if not isinstance(row, dict):
            errors.append(f"{where} must be an object")
            continue
        remote = row.get("remote_url")
        check(isinstance(row.get("branch"), str) and bool(row.get("branch")), f"{where}.branch is required")
        check(isinstance(row.get("sha"), str) and bool(SHA.fullmatch(row.get("sha") or "")),
              f"{where}.sha must be a 40-hex commit")
        if not isinstance(remote, str) or not remote:
            errors.append(f"{where}.remote_url is required")
        else:
            try:
                expected = repo_id(canonical_remote(remote))
            except ValueError:
                errors.append(f"{where}.remote_url is not a usable remote")
            else:
                if version == 4:
                    check(key == expected, f"{where} key must be repo_id(remote_url) = {expected}")
        if version == 4:
            check(isinstance(row.get("name"), str) and bool(row.get("name")), f"{where}.name is required")
        if "stale_since" in row:
            check_time(row["stale_since"], f"{where}.stale_since")
    if errors:
        raise CheckpointError("; ".join(errors))
    return checkpoint


def dump(value: Any, path: Path | None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"cannot read {path}: {exc}") from exc


def autopilot_runs(autopilot: str, cache_dir: Path | None) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        offset = page * PAGE_SIZE
        cache = cache_dir / "autopilot-runs" / f"offset-{offset:06d}.json" if cache_dir else None
        response = run_multica(
            ["autopilot", "runs", autopilot, "--limit", str(PAGE_SIZE), "--offset", str(offset), "--output", "json"],
            cache=cache,
        )
        rows = response.get("runs") if isinstance(response, dict) else response
        if not isinstance(rows, list):
            raise MulticaError("unexpected `multica autopilot runs` response")
        runs.extend(row for row in rows if isinstance(row, dict))
        total = response.get("total") if isinstance(response, dict) else None
        if len(rows) < PAGE_SIZE or (isinstance(total, int) and len(runs) >= total):
            return runs
    raise MulticaError(f"autopilot runs exceeded {MAX_PAGES} pages")


def run_issue_ids(runs: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for run in runs:
        issue_id = run.get("issue_id")
        if isinstance(issue_id, str) and issue_id and issue_id not in ids:
            ids.append(issue_id)
    return ids


def cmd_find(args: argparse.Namespace) -> int:
    runs = autopilot_runs(args.autopilot, args.cache_dir)
    issue_ids = run_issue_ids(runs)
    issue_ids += [issue for issue in args.seed_issue if issue not in issue_ids]
    # A run issue that can never be read again (e.g. deleted) would otherwise keep every later
    # find at exit 3. Excluding it is an explicit, reported operator decision.
    excluded = [issue for issue in issue_ids if issue in set(args.exclude_issue)]
    issue_ids = [issue for issue in issue_ids if issue not in set(args.exclude_issue)]
    found: dict[int, list[tuple[datetime, str, dict[str, Any]]]] = {4: [], 3: []}
    invalid: list[dict[str, str]] = []
    unreadable: list[dict[str, str]] = []
    for issue_id in issue_ids:
        cache = args.cache_dir / "issue-metadata" / f"{issue_id}.json" if args.cache_dir else None
        try:
            metadata = run_multica(["issue", "metadata", "list", issue_id, "--output", "json"], cache=cache)
        except MulticaError as exc:
            unreadable.append({"issue_id": issue_id, "error": str(exc)})
            continue
        if not isinstance(metadata, dict):
            unreadable.append({"issue_id": issue_id, "error": UNREADABLE_METADATA})
            continue
        for version, key in KEYS.items():
            if key not in metadata:
                continue
            try:
                checkpoint = validate(decode(metadata[key]), version)
            except CheckpointError as exc:
                invalid.append({"issue_id": issue_id, "key": key, "error": str(exc)})
                continue
            found[version].append((parse_time(checkpoint["completed_at"]), issue_id, checkpoint))
    result: dict[str, Any] = {
        "autopilot": args.autopilot,
        "runs": len(runs),
        "issues_considered": len(issue_ids),
        "valid": {"v4": len(found[4]), "v3": len(found[3])},
        "invalid": invalid,
        "unreadable": unreadable,
        "excluded": excluded,
    }
    version = 4 if found[4] else 3 if found[3] else None
    if unreadable and version != 4:
        # An unreadable run issue may hold the newest v4. Reporting "none" would bootstrap every
        # repo as new, and a v3 fallback would drop repos only that v4 tracks.
        dump({**result, "found": False}, args.output)
        return EXIT_FATAL
    if version is None:
        dump({**result, "found": False}, args.output)
        return EXIT_NOT_FOUND
    completed, issue_id, checkpoint = max(found[version], key=lambda item: (item[0], item[1]))
    result.update({
        "found": True,
        "version": version,
        "key": KEYS[version],
        "fallback": version == 3,
        "issue_id": issue_id,
        "completed_at": checkpoint["completed_at"],
        "issues_cursor": checkpoint["issues"],
        "repo_count": len(checkpoint["repos"]),
        "stale_repos": sorted(row.get("name", key) for key, row in checkpoint["repos"].items()
                              if row.get("stale_since")),
        "checkpoint": checkpoint,
    })
    dump(result, args.output)
    return EXIT_PARTIAL if unreadable else EXIT_OK


def issues_cursor(value: Any) -> dict[str, Any]:
    """Accept issue_delta output ({next_cursor}), find output ({checkpoint}), or a checkpoint."""
    if isinstance(value, dict) and isinstance(value.get("next_cursor"), dict):
        return value["next_cursor"]
    cursor = unwrap_checkpoint(value).get("issues")
    if not isinstance(cursor, dict):
        raise CheckpointError("no issues cursor found")
    return cursor


def cursor_key(cursor: Any, field: str) -> tuple[datetime, str]:
    if not isinstance(cursor, dict) or not isinstance(cursor.get("id"), str):
        raise CheckpointError(f"{field} must be an object {{updated_at, id}}")
    return parse_time(cursor.get("updated_at"), f"{field}.updated_at"), cursor["id"]


def baseline_decisions(args: argparse.Namespace, required: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Map repo_id -> disposition for every baseline_required scan row, or refuse."""
    def resolve(ref: str) -> str:
        matches = {row.get("repo_id") for row in required if ref in (row.get("repo_id"), row.get("name"))}
        if len(matches) != 1:
            raise CheckpointError(f"baseline {ref!r} matches {len(matches)} baseline_required repos; use its repo_id")
        return str(matches.pop())

    decisions: dict[str, dict[str, str]] = {}
    for ref in args.baseline_done:
        decisions[resolve(ref)] = {"disposition": "done"}
    for item in args.baseline_waive:
        ref, _, reason = item.partition("=")
        if not reason.strip():
            raise CheckpointError(f"--baseline-waive needs REPO=REASON: {item!r}")
        decisions[resolve(ref)] = {"disposition": "waived", "reason": reason.strip()}
    pending = sorted(str(row.get("name") or row.get("repo_id")) for row in required
                     if row.get("repo_id") not in decisions)
    if pending:
        raise CheckpointError(
            "baseline_required repos need --baseline-done or --baseline-waive REPO=REASON: " + ", ".join(pending)
        )
    return decisions


def cmd_build(args: argparse.Namespace) -> int:
    scan = load_json(args.scan)
    candidate = scan.get("checkpoint_candidate") if isinstance(scan, dict) else None
    scanned = scan.get("repos") if isinstance(scan, dict) else None
    # The full report is required: its rows carry previous_sha and baseline_required.
    if not isinstance(candidate, dict) or not isinstance(candidate.get("repos"), dict) or not isinstance(scanned, list):
        raise CheckpointError(f"{args.scan} is not a scanner report with repos and checkpoint_candidate")
    delta_start = None
    if args.issues_cursor:
        loaded_cursor = load_json(args.issues_cursor)
        cursor = issues_cursor(loaded_cursor)
        cursor_source = str(args.issues_cursor)
        if isinstance(loaded_cursor, dict) and "next_cursor" in loaded_cursor:
            delta_start = loaded_cursor.get("cursor", "missing")
    elif args.issues_updated_at or args.issues_id:
        cursor = {"updated_at": args.issues_updated_at, "id": args.issues_id}
        cursor_source = "arguments"
    else:
        cursor = candidate.get("issues")
        cursor_source = "unchanged"
    completed_at = args.completed_at or format_time(datetime.now(UTC).replace(microsecond=0))
    checkpoint = validate({
        "version": 4,
        "repo_root": candidate.get("repo_root"),
        "repos": candidate["repos"],
        "issues": {"updated_at": (cursor or {}).get("updated_at"), "id": (cursor or {}).get("id")},
        "completed_at": completed_at,
    })

    previous = None
    if args.previous:
        previous = unwrap_checkpoint(load_json(args.previous))
        version = previous.get("version") if previous.get("version") in KEYS else 4
        validate(previous, version)
        errors = []
        old_identities = {canonical_remote(row["remote_url"]) for row in previous["repos"].values()}
        new_identities = {canonical_remote(row["remote_url"]) for row in checkpoint["repos"].values()}
        lost = sorted(old_identities - new_identities)
        if lost:
            errors.append(f"repos missing from the new checkpoint: {', '.join(lost)}")
        # The scan must have been diffed against this checkpoint: scanned and failed rows
        # report it as previous_sha, unlisted rows are carried with the same sha.
        rows_by_id = {row.get("repo_id"): row for row in scanned if isinstance(row, dict)}
        mismatched = []
        for key, old in sorted(previous["repos"].items()):
            rid = repo_id(canonical_remote(old["remote_url"]))
            row = rows_by_id.get(rid)
            seen = row.get("previous_sha") if row else checkpoint["repos"].get(rid, {}).get("sha")
            if seen != old["sha"]:
                mismatched.append(str(old.get("name") or key))
        if mismatched:
            errors.append(f"scan was not computed against --previous: {', '.join(mismatched)}")
        old_cursor = cursor_key(previous["issues"], "previous issues")
        if cursor_key(checkpoint["issues"], "issues") < old_cursor:
            errors.append("issues cursor would move backwards")
        if delta_start is not None and cursor_key(delta_start, "issue delta cursor") > old_cursor:
            errors.append("issue delta starts after the previous issues cursor; issues in between would be skipped")
        if parse_time(checkpoint["completed_at"]) <= parse_time(previous["completed_at"]):
            errors.append("completed_at must be later than the previous checkpoint")
        if errors:
            raise CheckpointError("; ".join(errors))

    required = [row for row in scanned if isinstance(row, dict) and row.get("baseline_required")]
    decisions = baseline_decisions(args, required)
    for rid, decision in decisions.items():
        row = checkpoint["repos"].get(rid)
        if row is None:
            raise CheckpointError(f"baseline_required repo {rid} is not in checkpoint_candidate")
        # A new or rebaselined repo is covered at this sha only through an explicit decision.
        checkpoint["repos"][rid] = {**row, "baseline": {"sha": row["sha"], "at": completed_at, **decision}}

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    dump({
        "output": str(args.output),
        "completed_at": completed_at,
        "repo_count": len(checkpoint["repos"]),
        "issues_cursor": {
            "old": previous["issues"] if previous else candidate.get("issues"),
            "new": checkpoint["issues"],
            "source": cursor_source,
        },
        "stale_repos": sorted(row["name"] for row in checkpoint["repos"].values() if row.get("stale_since")),
        "baseline_required": [
            {"name": row.get("name"), "repo_id": row.get("repo_id"), "state": row.get("state"),
             "reason": row.get("rebaseline_reason"), "baseline": decisions[str(row.get("repo_id"))]}
            for row in required
        ],
        "warnings": dict(sorted(Counter(
            str(warning.get("type")) for warning in (scan.get("warnings") or []) if isinstance(warning, dict)
        ).items())),
    }, None)
    return EXIT_OK


def cmd_write(args: argparse.Namespace) -> int:
    checkpoint = validate(unwrap_checkpoint(load_json(args.file)))
    value = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    run_multica(["issue", "metadata", "set", args.issue, "--key", V4_KEY, "--value", value,
                 "--type", "string", "--output", "json"])
    readback = decode(run_multica(["issue", "metadata", "get", args.issue, "--key", V4_KEY, "--output", "json"]))
    if readback != checkpoint:
        raise MulticaError(f"readback of {V4_KEY} on issue {args.issue} does not match what was written")
    dump({
        "issue_id": args.issue,
        "key": V4_KEY,
        "verified": True,
        "completed_at": checkpoint["completed_at"],
        "issues_cursor": checkpoint["issues"],
        "repo_count": len(checkpoint["repos"]),
    }, None)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    find = sub.add_parser("find", help="latest valid checkpoint from an autopilot's run issues (read-only)")
    find.add_argument("--autopilot", required=True, help="autopilot id")
    find.add_argument("--seed-issue", action="append", default=[], help="extra issue to consider (repeatable)")
    find.add_argument("--exclude-issue", action="append", default=[], metavar="ISSUE_ID",
                      help="skip a run issue that is permanently unreadable, e.g. deleted (repeatable)")
    find.add_argument("--cache-dir", type=Path, help="store raw multica responses here")
    find.add_argument("--output", type=Path, help="write the JSON result here as well as stdout")
    find.set_defaults(handler=cmd_find)

    build = sub.add_parser("build", help="merge scanner candidate + issues cursor into a validated v4")
    build.add_argument("--scan", type=Path, required=True, help="scan_reference_repos.py JSON report")
    build.add_argument("--issues-cursor", type=Path, help="issue_delta output (next_cursor) or a checkpoint")
    build.add_argument("--issues-updated-at", help="explicit issues cursor timestamp")
    build.add_argument("--issues-id", help="explicit issues cursor issue id")
    build.add_argument("--completed-at", help="RFC 3339 completion time (default: now)")
    build.add_argument("--previous", type=Path, help="current checkpoint (or find output) to guard against loss")
    build.add_argument("--baseline-done", action="append", default=[], metavar="REPO",
                       help="repo_id or name of a baseline_required repo whose baseline was reviewed (repeatable)")
    build.add_argument("--baseline-waive", action="append", default=[], metavar="REPO=REASON",
                       help="baseline_required repo whose baseline is deliberately skipped, with the reason")
    build.add_argument("--output", type=Path, required=True, help="where to write the v4 checkpoint JSON")
    build.set_defaults(handler=cmd_build)

    write = sub.add_parser("write", help="set the v4 on an issue, read it back, and compare")
    write.add_argument("--issue", required=True, help="issue id to write")
    write.add_argument("--file", type=Path, required=True, help="v4 checkpoint JSON from build")
    write.set_defaults(handler=cmd_write)

    args = parser.parse_args(argv)
    if args.command == "build" and args.issues_cursor and (args.issues_updated_at or args.issues_id):
        parser.error("use either --issues-cursor or --issues-updated-at/--issues-id")
    try:
        return args.handler(args)
    except (ValueError, MulticaError, OSError) as exc:  # CheckpointError and JSON errors are ValueErrors
        print(f"checkpoint {args.command}: {exc}", file=sys.stderr)
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
