#!/usr/bin/env python3
"""Enforce data-preservation invariants when an OKF concept is updated.

The #1 update hazard: an LLM re-writing a concept clobbers provenance (drops a
source / tag / Citation) or silently summarizes the body away. This makes updates
safe via a deterministic step run AROUND the agent's edit:

    1. snapshot  — BEFORE editing: back up the concept to .okf/history/<name>-<ts>.md
    2. (the agent edits the concept's prose)
    3. enforce   — AFTER editing: diff against the snapshot and enforce:
         * array-union frontmatter lists (tags, sources, contradictions) and the
           body "# Citations" bullets — never drop a prior entry (union, current
           order first, missing prior entries appended)
         * lock identity fields (type, title, timestamp) to prior values
           (--allow-retype permits a deliberate type/title change)
         * stamp last_verified_at = today
         * body-shrink guard: if the new body < 70% of prior AND Citations did not
           grow, demote status -> draft and warn (catches summarize-away);
           --allow-shrink overrides

`enforce` HARD-FAILS (exit 2) if it cannot find a prior snapshot for an existing
concept — the invariants would otherwise be silent no-ops.

Deterministic, Python3 + PyYAML + stdlib only.

Usage:
    update_concept.py snapshot <bundle> <concept-rel-path>
    update_concept.py enforce  <bundle> <concept-rel-path> [--prior <path>] [--allow-retype] [--allow-shrink]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

DELIM = "---"
LIST_FIELDS = ["tags", "sources", "contradictions"]
LOCK_FIELDS = ["type", "title", "timestamp"]
HISTORY = ".okf/history"
KEEP_BACKUPS = 10
SHRINK_RATIO = 0.70


class _Loader(yaml.SafeLoader):
    pass


# Keep timestamps as plain strings — otherwise PyYAML parses `2026-06-17T00:00:00Z`
# into a datetime and re-dumps it as `2026-06-17 00:00:00+00:00`, churning the field.
_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class _Dumper(yaml.SafeDumper):
    pass


# Mirror the loader: don't treat date-like strings as timestamps, so we emit them
# plain (e.g. 2026-06-17T00:00:00Z) instead of quoting them — keeps diffs minimal.
_Dumper.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeDumper.yaml_implicit_resolvers.items()
}


def _repr_list(dumper, data):
    # Keep scalar lists inline (tags: [a, b]); block-style otherwise.
    flow = all(isinstance(x, (str, int, float, bool)) for x in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_Dumper.add_representer(list, _repr_list)


def _parse(text: str):
    """Return (frontmatter dict, body str). Raises if no frontmatter block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != DELIM:
        raise ValueError("no YAML frontmatter")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == DELIM:
            end = i
            break
    if end is None:
        raise ValueError("unterminated frontmatter")
    fm = yaml.load("\n".join(lines[1:end]), Loader=_Loader) or {}
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a mapping")
    body = "\n".join(lines[end + 1:])
    return fm, body


def _serialize(fm: dict, body: str) -> str:
    dumped = yaml.dump(fm, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=4096)
    out = f"{DELIM}\n{dumped}{DELIM}\n{body}"
    return out if out.endswith("\n") else out + "\n"


def _union(cur, prior):
    cur = cur if isinstance(cur, list) else ([] if cur is None else [cur])
    prior = prior if isinstance(prior, list) else ([] if prior is None else [prior])
    out, seen = [], set()
    for x in list(cur) + list(prior):
        k = str(x)  # exact, case-sensitive — never collapse case-distinct ids/paths
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


_CITE_HEADING = "# Citations"


def _cite_section(body: str):
    """Return (lines, start, end) for the '# Citations' section. start = the heading
    line index, end = index of the NEXT heading of any level (or EOF). start/end are
    None when there is no Citations section."""
    lines = body.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == _CITE_HEADING:
            start = i
            break
    if start is None:
        return lines, None, None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].lstrip().startswith("#"):  # any heading terminates the section
            end = j
            break
    return lines, start, end


def _entry_lines(section_lines):
    """Citation entries within a section: '- '/'* ' bullets or 'N.'/'N)' numbered."""
    out = []
    for ln in section_lines:
        s = ln.strip()
        if s.startswith(("- ", "* ")) or re.match(r"\d+[.)]\s", s):
            out.append(ln)
    return out


def _history_paths(root: Path, rel: str):
    name = rel.replace("/", "__")
    hdir = root / HISTORY
    return hdir, name, sorted(hdir.glob(f"{name}-*.md")) if hdir.exists() else []


def _prune(root: Path, rel: str):
    _, _, backups = _history_paths(root, rel)
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink()


def cmd_snapshot(root: Path, rel: str) -> int:
    src = root / rel
    if not src.is_file():
        print(f"error: concept not found: {rel}", file=sys.stderr)
        return 2
    hdir, name, _ = _history_paths(root, rel)
    hdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")  # microsecond → unique within a second
    dest = hdir / f"{name}-{ts}.md"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    _prune(root, rel)
    print(f"snapshot → {dest.relative_to(root)}")
    return 0


def cmd_enforce(root: Path, rel: str, prior_path, allow_retype, allow_shrink) -> int:
    cur_file = root / rel
    if not cur_file.is_file():
        print(f"error: concept not found: {rel}", file=sys.stderr)
        return 2

    if prior_path:
        prior_file = Path(prior_path).expanduser()
    else:
        _, _, backups = _history_paths(root, rel)
        prior_file = backups[-1] if backups else None
    if not prior_file or not prior_file.is_file():
        print(f"error: no prior snapshot for {rel}. Run "
              f"`update_concept.py snapshot <bundle> {rel}` BEFORE editing.",
              file=sys.stderr)
        return 2

    pfm, pbody = _parse(prior_file.read_text(encoding="utf-8"))
    cfm, cbody = _parse(cur_file.read_text(encoding="utf-8"))
    notes = []

    # 1. citation entries + shrink metric (on the agent's raw body, before re-union)
    pl, ps, pe = _cite_section(pbody)
    cl, cs, ce = _cite_section(cbody)
    p_entries = _entry_lines(pl[ps + 1:pe]) if ps is not None else []
    c_entries = _entry_lines(cl[cs + 1:ce]) if cs is not None else []
    p_cit, c_cit = len(p_entries), len(c_entries)
    shrank = len(pbody) > 0 and len(cbody) < SHRINK_RATIO * len(pbody) and c_cit <= p_cit

    # 2. array-union frontmatter lists
    for f in LIST_FIELDS:
        if f in pfm or f in cfm:
            merged = _union(cfm.get(f), pfm.get(f))
            if merged:
                before = cfm.get(f)
                cfm[f] = merged
                if before != merged:
                    notes.append(f"union {f}: {before!r} + prior → {merged!r}")

    # 3. lock identity fields
    for f in LOCK_FIELDS:
        if f in pfm and cfm.get(f) != pfm[f]:
            if allow_retype and f in ("type", "title"):
                notes.append(f"RETYPE allowed: {f} {pfm[f]!r} → {cfm.get(f)!r} "
                             f"(remember to move the file / rewrite inbound links)")
            else:
                notes.append(f"locked {f}: restored {pfm[f]!r} (was {cfm.get(f)!r})")
                cfm[f] = pfm[f]

    # 4. stamp verification time
    cfm["last_verified_at"] = date.today().isoformat()

    # 5. citation union: keep ALL current Citations content (prose, blanks, subsections)
    #    and only APPEND prior entries that were dropped — never reconstruct from bullets.
    if ps is not None and cs is not None:
        cur_norm = {e.strip() for e in c_entries}
        missing = [e for e in p_entries if e.strip() not in cur_norm]
        if missing:
            ins = ce  # insert before the next heading, skipping trailing blank lines
            while ins - 1 > cs and cl[ins - 1].strip() == "":
                ins -= 1
            cbody = "\n".join(cl[:ins] + missing + cl[ins:])
            notes.append(f"re-added {len(missing)} dropped citation(s)")
    elif ps is not None and cs is None:
        cbody = cbody.rstrip() + "\n\n# Citations\n\n" + "\n".join(p_entries) + "\n"
        notes.append("restored missing # Citations section from prior")

    # 6. body-shrink guard
    if shrank and not allow_shrink:
        if cfm.get("status") != "draft":
            notes.append(f"BODY SHRANK >{int((1-SHRINK_RATIO)*100)}% with no citation growth "
                         f"→ status demoted to draft (was {cfm.get('status')!r}; --allow-shrink to keep)")
            cfm["status"] = "draft"
        else:
            notes.append("body shrank >30% with no citation growth (status already draft)")

    cur_file.write_text(_serialize(cfm, cbody), encoding="utf-8")
    print(f"enforced invariants on {rel} (prior: {prior_file.name})")
    for n in notes:
        print(f"  • {n}")
    if not notes:
        print("  • no changes needed (already consistent)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Enforce update invariants on an OKF concept.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="back up a concept BEFORE editing")
    s.add_argument("bundle", type=Path)
    s.add_argument("concept")
    e = sub.add_parser("enforce", help="enforce invariants AFTER editing")
    e.add_argument("bundle", type=Path)
    e.add_argument("concept")
    e.add_argument("--prior", default=None, help="prior snapshot path (default: latest in .okf/history)")
    e.add_argument("--allow-retype", action="store_true")
    e.add_argument("--allow-shrink", action="store_true")
    a = ap.parse_args(argv)

    root = a.bundle.expanduser().resolve()
    if not root.is_dir():
        ap.error(f"not a bundle directory: {root}")
    rel = a.concept.replace("\\", "/").strip("/")

    if a.cmd == "snapshot":
        return cmd_snapshot(root, rel)
    return cmd_enforce(root, rel, a.prior, a.allow_retype, a.allow_shrink)


if __name__ == "__main__":
    raise SystemExit(main())
