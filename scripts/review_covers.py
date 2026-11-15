#!/usr/bin/env python3
"""Does the task's review record approve exactly the code being released? Stdlib only, read-only.

``auto_release.sh`` runs its ``AUTO_RELEASE_REVIEW`` command with the release head appended; this is
that command's second half. The control repository's review gate (``check-review-rounds.js``) proves
the rounds chain and nothing blocking is open, but never compares what was reviewed with what is
about to ship. This does:

    review_covers.py <reviews-dir> <repo-dir> <repo-id> <head>

Exit 0 when the latest ``round-N.json`` in ``<reviews-dir>`` has verdict APPROVED, no open P1 or P2
finding, and its ``reviewedHeads[<repo-id>]`` names a commit whose tree is ``<head>``'s tree (a merge
of the reviewed commit into the baseline or the release branch keeps the tree; anything committed
after the review changes it). Exit 3 with the reason otherwise; 2 when a file or commit cannot be read.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


def tree(repo: str, commit: str) -> str | None:
    done = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}"],
                          capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def covers(reviews: Path, repo: str, repo_id: str, head: str) -> tuple[int, str]:
    rounds = sorted((int(match.group(1)), path) for path in reviews.glob("round-*.json")
                    if (match := re.fullmatch(r"round-(\d+)\.json", path.name)))
    if not rounds:
        return 3, f"no review round in {reviews}"
    number, path = rounds[-1]
    try:
        last = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 2, f"{path.name}: {exc}"
    if last.get("verdict") != "APPROVED":
        return 3, f"round {number} ended {last.get('verdict')}, not APPROVED"
    blocking = [str(finding.get("id")) for finding in last.get("findings") or []
                if isinstance(finding, dict) and finding.get("status") == "open"
                and finding.get("severity") in ("P1", "P2")]
    if blocking:
        return 3, f"round {number} leaves {', '.join(blocking)} open"
    reviewed = (last.get("reviewedHeads") or {}).get(repo_id)
    if not isinstance(reviewed, str) or not re.fullmatch(r"[0-9a-f]{7,40}", reviewed):
        return 3, f"round {number} names no reviewed head for {repo_id}"
    want, got = tree(repo, head), tree(repo, reviewed)
    if want is None:
        return 2, f"{head} is not a commit in {repo}"
    if got is None:
        return 2, f"the reviewed head {reviewed} is not a commit in {repo}"
    if want != got:
        return 3, f"round {number} reviewed {reviewed[:7]}; the release {head[:7]} holds other code"
    return 0, f"round {number} approved {reviewed[:7]}, the code of {head[:7]}"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: review_covers.py <reviews-dir> <repo-dir> <repo-id> <head>", file=sys.stderr)
        return 2
    code, message = covers(Path(argv[0]), argv[1], argv[2], argv[3])
    print(f"review_covers: {message}", file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
