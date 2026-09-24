"""Serve a recorded Multica snapshot through the read-only subset of the `multica` CLI.

Usage: fake_multica.py <snapshot.json> <multica args...>. Every call is appended to
$FAKE_MULTICA_LOG (one JSON argv per line) so tests can assert the watchdog stays read-only.
Issue listings are paged at 20 rows to exercise pagination; issues marked "_reassigned"
are absent from the assignee listing and only reachable through `issue get`.
"""
import json
import os
import sys

PAGE = 20


def option(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def main() -> int:
    snapshot = json.load(open(sys.argv[1], encoding="utf-8"))
    args = sys.argv[2:]
    if os.environ.get("FAKE_MULTICA_LOG"):
        with open(os.environ["FAKE_MULTICA_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps(args) + "\n")
    issues = {issue["id"]: issue for issue in snapshot["issues"]}
    command = args[:2]
    if command == ["autopilot", "runs"]:
        runs = sorted(snapshot["runs"], key=lambda r: r["created_at"], reverse=True)
        out = {"runs": runs[: int(option(args, "--limit", 20))]}
    elif command == ["autopilot", "get"]:
        out = {"autopilot": snapshot["autopilot"]}
    elif command == ["issue", "list"]:
        listed = [i for i in snapshot["issues"] if not i.get("_reassigned")]
        offset = int(option(args, "--offset", 0))
        limit = min(int(option(args, "--limit", 50)), PAGE)
        page = listed[offset: offset + limit]
        out = {"issues": page, "has_more": offset + len(page) < len(listed), "total": len(listed)}
    elif command == ["issue", "get"] and args[2] in issues:
        out = issues[args[2]]
    elif command == ["issue", "timeline"]:
        out = snapshot["timelines"].get(args[2], [])
    else:
        print(f"unsupported: {args}", file=sys.stderr)
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
