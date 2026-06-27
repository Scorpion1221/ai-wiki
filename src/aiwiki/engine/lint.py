#!/usr/bin/env python3
"""Health-audit an OKF bundle: the structural/graph checks no other script does.

Complements (does not duplicate) the other tools:
  * validate_okf_bundle.py — frontmatter / Citations / broken-link GATE.
  * scan_sources.py        — source sha256 drift (lint points here, doesn't redo it).
  * lint.py (this)         — the AUDITOR: orphans, index completeness, contradictions
                             consistency, tag-taxonomy, page size, stale / verification lag.

Read-only. Deterministic, Python3 + PyYAML + stdlib only. Exits 1 if any HIGH
finding (broken links / dangling contradictions), else 0 — so it is CI-usable.

Usage:
    lint.py <bundle> [--json]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

RESERVED = {"index.md", "log.md", "SCHEMA.md", "purpose.md"}
SKIP_TOP = {"sources", ".okf"}
DELIM = "---"
LINK_RE = re.compile(r"\]\(([^)\s]+\.md)(?:#[^)]*)?\)")
PAGE_LIMIT = 200
LOG_ROTATE_NEAR = 480
VALID_STATUS = {"draft", "reviewed", "canonical", "stale"}
DEFAULT_LEDGER = ".okf/health.jsonl"


def _parse(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != DELIM:
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == DELIM:
            try:
                fm = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                fm = {}
            return (fm if isinstance(fm, dict) else {}), "\n".join(lines[i + 1:])
    return {}, text


def _concepts(root):
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if p.name in RESERVED or p.name.startswith("log-") or rel.parts[0] in SKIP_TOP:
            continue
        yield p, rel.as_posix()


def _norm(s):
    return re.sub(r"[\s_-]+", "", s.lower())


def _schema_tags(root):
    sf = root / "SCHEMA.md"
    if not sf.is_file():
        return None
    text = sf.read_text(encoding="utf-8")
    m = re.search(r"#+\s*Tag taxonomy(.+?)(?:\n#|\Z)", text, re.S | re.I)
    if not m:
        return None
    tags = set()
    for span in re.findall(r"`([^`]+)`", m.group(1)):
        for tok in re.split(r"[,\s]+", span):
            tok = tok.strip()
            if tok:
                tags.add(tok)
    return tags or None


def lint(root):
    concepts = {}  # rel -> (fm, body, line_count)
    for p, rel in _concepts(root):
        fm, body = _parse(p.read_text(encoding="utf-8"))
        concepts[rel] = (fm, body, len(body.splitlines()))
    ids = set(concepts)
    norm_index = {}
    for rel in ids:
        norm_index.setdefault(_norm(Path(rel).name[:-3]), []).append(rel)

    inbound = {rel: 0 for rel in ids}
    findings = []

    def add(sev, check, where, detail, suggestion=""):
        findings.append({"severity": sev, "check": check, "where": where,
                         "detail": detail, "suggestion": suggestion})

    # --- link graph: broken links + inbound counts (body + Related concepts) ---
    for rel, (_fm, body, _) in concepts.items():
        cdir = (root / rel).parent
        for m in LINK_RE.finditer(body):
            target = m.group(1)
            if "://" in target or target.startswith("/"):
                continue
            try:
                resolved = (cdir / target).resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                add("high", "broken-link", rel, f"link escapes bundle: {target}")
                continue
            if resolved in ids:
                inbound[resolved] += 1
            else:
                hit = norm_index.get(_norm(Path(target).name[:-3]), [])
                sug = f"did you mean {hit[0]}?" if len(hit) == 1 else ""
                add("high", "broken-link", rel, f"unresolved link: {target}", sug)

    # --- orphans (no inbound concept link) ---
    for rel in sorted(ids):
        if inbound[rel] == 0:
            add("medium", "orphan", rel, "no inbound links from other concepts",
                "add a `# Related concepts` link from a related page")

    # --- index completeness (concept listed in its dir index; entries resolve) ---
    for rel in sorted(ids):
        idx = (root / rel).parent / "index.md"
        if not idx.is_file():
            add("medium", "index", rel, "directory has no index.md",
                "run gen_indexes.py")
            continue
        if f"({Path(rel).name})" not in idx.read_text(encoding="utf-8"):
            add("medium", "index", rel, "concept not listed in its directory index.md",
                "run gen_indexes.py")
    for idx in sorted(root.rglob("index.md")):
        if ".okf" in idx.relative_to(root).parts:
            continue
        for m in LINK_RE.finditer(idx.read_text(encoding="utf-8")):
            t = m.group(1)
            if "://" in t or t.startswith("/"):
                continue
            try:
                r = (idx.parent / t).resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
            if not (root / r).exists():
                add("medium", "index", idx.relative_to(root).as_posix(),
                    f"index entry points to missing file: {t}", "run gen_indexes.py")

    # --- contradictions consistency (resolve, reciprocity, contested<->contradictions) ---
    contra = {}
    for rel, (fm, _, _) in concepts.items():
        c = fm.get("contradictions") or []
        if isinstance(c, str):
            c = [c]
        targets = []
        for t in c:
            cdir = (root / rel).parent
            try:
                r = (cdir / str(t)).resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                r = None
            if r is None or r not in ids:
                add("high", "contradiction", rel, f"contradictions target missing: {t}")
            else:
                targets.append(r)
        contra[rel] = targets
        if fm.get("contested") and not c:
            add("medium", "contradiction", rel,
                "contested: true but contradictions: is empty",
                "list the conflicting concept(s) or clear contested")
    for rel, targets in contra.items():
        for t in targets:
            if rel not in contra.get(t, []):
                add("medium", "contradiction", rel,
                    f"non-reciprocal: lists {t}, but {t} does not list it back",
                    "add the reciprocal contradictions entry (or resolve both)")

    # --- status / verification ---
    for rel, (fm, _, _) in concepts.items():
        st = fm.get("status")
        if st == "stale":
            add("medium", "stale", rel, "status: stale — needs re-verification")
        elif st and st not in VALID_STATUS:
            add("medium", "frontmatter", rel, f"invalid status: {st!r}")
        lv, su = str(fm.get("last_verified_at") or ""), str(fm.get("source_updated_at") or "")
        if lv and su and lv < su:
            add("low", "verification-lag", rel,
                f"last_verified_at ({lv}) older than source_updated_at ({su})",
                "re-verify against the source and re-stamp")

    # --- tag taxonomy ---
    allowed = _schema_tags(root)
    if allowed:
        for rel, (fm, _, _) in concepts.items():
            tags = fm.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            for t in tags:
                if str(t) not in allowed:
                    add("low", "tag", rel, f"tag not in SCHEMA.md taxonomy: {t}",
                        "add it to SCHEMA.md or retag")

    # --- page size ---
    for rel, (_, _, n) in concepts.items():
        if n > PAGE_LIMIT:
            add("low", "page-size", rel, f"{n} lines (> {PAGE_LIMIT}) — consider splitting")

    # --- structural file presence + log rotation ---
    for fn in ("SCHEMA.md", "purpose.md"):
        f = root / fn
        if not f.is_file() or not f.read_text(encoding="utf-8").strip():
            add("low", "presence", fn, "missing or empty (Layer-3 contract file)")
    log = root / "log.md"
    if not log.is_file():
        add("low", "presence", "log.md", "no change ledger — use append_log.py")
    else:
        n = sum(1 for ln in log.read_text(encoding="utf-8").splitlines() if ln.startswith("## ["))
        if n >= LOG_ROTATE_NEAR:
            add("low", "log", "log.md", f"{n} entries — nearing rotation (500)")

    return findings, len(ids)


def _ledger_path(root, value):
    p = Path(value).expanduser()
    return p if p.is_absolute() else root / value


def _append_health(ledger, findings, n_concepts):
    sev = Counter(f["severity"] for f in findings)
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "concepts": n_concepts,
        "high": sev.get("high", 0),
        "medium": sev.get("medium", 0),
        "low": sev.get("low", 0),
        "checks": dict(sorted(Counter(f["check"] for f in findings).items())),
    }
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _print_trend(ledger, n=12):
    if not ledger.exists():
        print(f"no health ledger yet at {ledger} (run with --log to start one)")
        return
    rows = []
    for ln in ledger.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    if not rows:
        print(f"health ledger {ledger} is empty")
        return
    print(f"── health trend ({ledger}) — last {min(n, len(rows))} of {len(rows)} ──")
    print(f"{'ts':<20}{'concepts':>9}{'H':>4}{'M':>4}{'L':>5}   Δ vs prev")
    prev = None
    for r in rows[-n:]:
        delta = ""
        if prev:
            parts = [f"{lab}{r.get(k,0)-prev.get(k,0):+d}"
                     for k, lab in (("high", "H"), ("medium", "M"), ("low", "L"))
                     if r.get(k, 0) - prev.get(k, 0)]
            delta = "  ".join(parts)
        print(f"{r.get('ts',''):<20}{r.get('concepts',0):>9}{r.get('high',0):>4}"
              f"{r.get('medium',0):>4}{r.get('low',0):>5}   {delta}")
        prev = r


def main(argv=None):
    ap = argparse.ArgumentParser(description="Health-audit an OKF bundle (structural checks).")
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", nargs="?", const=DEFAULT_LEDGER, default=None, metavar="PATH",
                    help=f"append a snapshot of the counts to the health ledger (default: {DEFAULT_LEDGER})")
    ap.add_argument("--trend", nargs="?", const=DEFAULT_LEDGER, default=None, metavar="PATH",
                    help="print recent health-ledger history with deltas, then exit")
    a = ap.parse_args(argv)

    root = a.bundle.expanduser().resolve()
    if not root.is_dir():
        ap.error(f"not a bundle directory: {root}")

    findings, n_concepts = lint(root)
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["check"], f["where"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "low")}

    if a.log is not None:
        _append_health(_ledger_path(root, a.log), findings, n_concepts)

    if a.json:
        print(json.dumps({"concepts": n_concepts, "findings": findings}, ensure_ascii=False, indent=2))
    else:
        print(f"OKF lint — {n_concepts} concepts · "
              f"{counts['high']} high · {counts['medium']} medium · {counts['low']} low\n")
        cur = None
        for f in findings:
            if f["severity"] != cur:
                cur = f["severity"]
                print(f"── {cur.upper()} ──")
            line = f"  [{f['check']}] {f['where']}: {f['detail']}"
            if f["suggestion"]:
                line += f"  → {f['suggestion']}"
            print(line)
        print("✓ clean" if not findings else "\n(source drift is checked separately — run scan_sources.py)")
        if a.log is not None:
            print(f"✓ logged snapshot to {a.log}")

    if a.trend is not None:
        print()
        _print_trend(_ledger_path(root, a.trend))

    return 1 if counts["high"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
