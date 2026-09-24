#!/usr/bin/env python3
"""Stand-in for the ``multica`` CLI used by maintainer script tests.

State lives in the JSON file named by FAKE_MULTICA_STATE; every call is appended to
``<state>.calls`` as a JSON argv list. Response shapes follow the real CLI (0.4.35):
``autopilot runs`` -> {runs, total}; ``issue list`` -> {issues, total, has_more, limit,
offset}, capped at ``page_cap`` rows whatever --limit says (production caps at 100 and
echoes the requested limit); ``order_by_offset`` replaces the listing order for one offset
to simulate a deletion or an unstable order among created_at ties; ``issue metadata list``
-> {key: stored value}; ``issue metadata get`` -> the stored value JSON-encoded once more;
``issue comment list`` -> [comments].
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime


def option(args: list[str], name: str, default: str | None = None) -> str | None:
    return args[args.index(name) + 1] if name in args else default


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    state_path = os.environ["FAKE_MULTICA_STATE"]
    with open(state_path, encoding="utf-8") as handle:
        state = json.load(handle)
    args = sys.argv[1:]
    with open(state_path + ".calls", "a", encoding="utf-8") as log:
        log.write(json.dumps(args) + "\n")
    for prefix, message in state.get("fail", {}).items():
        if " ".join(args).startswith(prefix):
            print(message, file=sys.stderr)
            return 1
    limit = int(option(args, "--limit", "50") or 50)
    offset = int(option(args, "--offset", "0") or 0)
    if args[:2] == ["autopilot", "runs"]:
        runs = state.get("runs", [])
        print(json.dumps({"runs": runs[offset:offset + limit], "total": len(runs)}))
    elif args[:2] == ["issue", "list"]:
        issues = sorted(state.get("issues", []), key=lambda row: row["created_at"],
                        reverse=option(args, "--direction") == "desc")
        order = state.get("order_by_offset", {}).get(str(offset))
        if order:
            by_id = {row["id"]: row for row in issues}
            issues = [by_id[issue_id] for issue_id in order]
        page = issues[offset:offset + min(limit, state.get("page_cap") or limit)]
        print(json.dumps({"issues": page, "total": len(issues),
                          "has_more": offset + len(page) < len(issues), "limit": limit, "offset": offset}))
    elif args[:3] == ["issue", "metadata", "list"]:
        print(json.dumps(state.get("metadata", {}).get(args[3], {})))
    elif args[:3] == ["issue", "metadata", "get"]:
        stored = state.get("metadata", {}).get(args[3], {})
        key = option(args, "--key")
        if key not in stored:
            print(f"Error: metadata key {key!r} not found", file=sys.stderr)
            return 1
        print(json.dumps(state.get("readback", {}).get(args[3], stored[key])))
    elif args[:3] == ["issue", "metadata", "set"]:
        value = option(args, "--value") or ""
        if option(args, "--type") != "string":
            value = json.loads(value)
        state.setdefault("metadata", {}).setdefault(args[3], {})[option(args, "--key")] = value
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        print(json.dumps({"issue_id": args[3], "key": option(args, "--key")}))
    elif args[:3] == ["issue", "comment", "list"]:
        since = option(args, "--since")
        comments = state.get("comments", {}).get(args[3], [])
        print(json.dumps([row for row in comments if not since or moment(row["created_at"]) > moment(since)]))
    else:
        print(f"fake multica: unsupported command {args}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
