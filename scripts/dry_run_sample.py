#!/usr/bin/env python3
"""Dry-run a sample of a bundle's concepts through the writer's changeset gate.

Pulls the published bundle once. For each sampled concept, appends one footnoted claim that
cites an uploaded probe packet and sends ``POST /changesets?dry_run=true`` exactly once. There
is no resend, unlike ``ai-wiki propose``, so every 5xx is counted. A dry-run creates no job,
takes no lock and never touches Git. Prints one JSON summary. Exits 1 when any answer is a 5xx
or never came, or when nothing could be sent.

Run it from a checkout, with the CLI configured for the endpoint and a token whose principal
may upload evidence (a human: token, e.g. the owner's), kept out of the shell history:

    read -rs AIWIKI_TOKEN && export AIWIKI_TOKEN
    uv run python scripts/dry_run_sample.py -b solvely-wiki --count 60 > dry-run.json

docs/phase2-shadow-runbook.md runs it against production before and after the shadow.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from aiwiki.cli import main as cli
from aiwiki.cli import workspace
from aiwiki.engine.validate import should_check

PROBE_ID = "dry-run-probe"
PROBE = b"# Dry-run probe\n\nA gate probe: the writer judges it and commits nothing.\n"


def cite(text: str) -> str | None:
    """``text`` citing the probe in its own ``sources`` list style, plus one footnoted claim;
    None when the concept has no block-style ``sources`` list to extend."""
    lines = text.splitlines(keepends=True)
    if "sources:\n" not in lines:
        return None
    start = lines.index("sources:\n")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"[A-Za-z_]+:|---", lines[i])), start + 1)
    if end == start + 1:
        return None
    indent = re.match(r" *", lines[start + 1]).group()
    lines.insert(end, f"{indent}- {{id: {PROBE_ID}, resource: evidence:packet}}\n")
    return "".join(lines).rstrip("\n") + f"\n\nThe gate was probed.[^{PROBE_ID}]\n\n[^{PROBE_ID}]: dry-run probe\n"


def sample(bundle: str, count: int, seed: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root, probe = Path(tmp) / "ws", Path(tmp) / "probe.md"
        probe.write_bytes(PROBE)
        workspace.pull(root, bundle)
        state = workspace.load(root)
        concepts = sorted(rel for rel, path in workspace._files(root, workspace=True).items()
                          if should_check(path, root) and not rel.startswith("sources/"))
        rows = []
        for rel in random.Random(seed).sample(concepts, min(count, len(concepts))):
            path = root / rel
            original = path.read_bytes()
            edited = cite(original.decode("utf-8"))
            if edited is None:
                rows.append({"path": rel, "skipped": "no block-style sources list"})
                continue
            path.write_bytes(edited.encode("utf-8"))
            try:
                request, _evidence = workspace.build(root, state, upload=probe, source_id=PROBE_ID)
            finally:
                path.write_bytes(original)
            started = time.monotonic()
            try:
                status, _headers, body = cli._http("POST", "/changesets", bundle=bundle, params={"dry_run": "true"},
                                                   data=json.dumps(request).encode("utf-8"), timeout=120)
            except OSError as exc:
                status, body = None, json.dumps({"error": f"network: {exc}"}).encode()
            answer = workspace._json(body)
            rows.append({"path": rel, "http": status, "status": answer.get("status"),
                         "codes": sorted({str(error.get("code")) for error in answer.get("errors") or []}),
                         "seconds": round(time.monotonic() - started, 1)})
    sent = [row for row in rows if "skipped" not in row]
    return {"bundle": bundle, "base_revision": state["base_revision"], "sent": len(sent),
            "skipped": len(rows) - len(sent), "http": dict(Counter(str(row["http"]) for row in sent)),
            "server_errors": [row for row in sent if row["http"] is None or row["http"] >= 500], "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-b", "--bundle", required=True)
    parser.add_argument("--count", type=int, default=40, help="concepts to sample (default 40)")
    parser.add_argument("--seed", type=int, default=0, help="sampling seed (default 0)")
    args = parser.parse_args(argv)
    try:
        summary = sample(args.bundle, args.count, args.seed)
    except workspace.WorkspaceError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 1 if summary["server_errors"] or not summary["sent"] else 0


if __name__ == "__main__":
    sys.exit(main())
