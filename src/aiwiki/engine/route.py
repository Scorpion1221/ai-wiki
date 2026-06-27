#!/usr/bin/env python3
"""Route an incoming document to the right OKF bundle by CONTENT — no human prompt
unless genuinely ambiguous.

Given a kb-root holding several human-created bundles (each a directory with a
purpose.md), this scores a document against every bundle from three signals and
emits a ranked shortlist + a decision:

  * scope match  — purpose.md `## Scope` In:/Out: bullets. Out: hit = hard penalty,
                   In: hit = strong vote. (Highest weight: scope is the stable axis.)
  * vocabulary   — weighted token overlap of the doc against each bundle's vocabulary
                   (purpose.md + SCHEMA.md tag taxonomy + concept titles). ASCII
                   identifiers (event/table names) weigh 2x; CJK handled via bigrams.
  * provenance   — doc filename stem already present in a bundle's sources/ (re-ingest).

Decision: if the top bundle clears MIN_SCORE and beats the runner-up by MARGIN,
it is CONFIDENT → the agent ingests there WITHOUT asking. Otherwise AMBIGUOUS →
the agent reads the top candidates' purpose.md and decides, asking the human only
if still torn. Exit 0 (confident or single bundle) / 3 (ambiguous) / 2 (error).

Deterministic, Python3 + PyYAML + stdlib only. No LLM, no network.

Usage:
    route.py <kb-root> <document-file> [--top N] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

DELIM = "---"
MIN_SCORE = 0.12      # top bundle must clear this to auto-route
MARGIN = 0.06         # …and beat the runner-up by this
_CJK = r"一-鿿"


def _tokens(text):
    """Weighted token bag: ASCII/identifier words (weight 2) + CJK bigrams (weight 1)."""
    text = text.lower()
    w = {}
    for m in re.findall(r"[a-z0-9_]{2,}", text):
        w[m] = max(w.get(m, 0), 2)
    for run in re.findall(f"[{_CJK}]+", text):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            w[bg] = max(w.get(bg, 0), 1)
    return w


def _frontmatter_and_body(text):
    lines = text.splitlines()
    if lines and lines[0].strip() == DELIM:
        for i in range(1, len(lines)):
            if lines[i].strip() == DELIM:
                try:
                    fm = yaml.safe_load("\n".join(lines[1:i])) or {}
                except yaml.YAMLError:
                    fm = {}
                return (fm if isinstance(fm, dict) else {}), "\n".join(lines[i + 1:])
    return {}, text


def _scope(purpose_text):
    """Return (in_text, out_text) from purpose.md `## Scope` In:/Out: bullets."""
    m = re.search(r"#+\s*Scope(.+?)(?:\n#|\Z)", purpose_text, re.S | re.I)
    in_t, out_t = "", ""
    if m:
        for ln in m.group(1).splitlines():
            s = re.sub(r"[*_\-`]", "", ln).strip()
            mi = re.match(r"(?i)in\s*[:：](.+)", s)
            mo = re.match(r"(?i)out\s*[:：](.+)", s)
            if mi:
                in_t += " " + mi.group(1)
            elif mo:
                out_t += " " + mo.group(1)
    return in_t, out_t


def _schema_tags_text(schema_text):
    m = re.search(r"#+\s*Tag taxonomy(.+?)(?:\n#|\Z)", schema_text, re.S | re.I)
    return " ".join(re.findall(r"`([^`]+)`", m.group(1))) if m else ""


def _concept_titles(bundle):
    titles = []
    for p in bundle.rglob("*.md"):
        rel = p.relative_to(bundle).parts
        if p.name in {"index.md", "log.md", "SCHEMA.md", "purpose.md"} or rel[0] in {"sources", ".okf"}:
            continue
        fm, _ = _frontmatter_and_body(p.read_text(encoding="utf-8", errors="replace"))
        if fm.get("title"):
            titles.append(str(fm["title"]))
    return " ".join(titles)


def _source_stems(bundle):
    sdir = bundle / "sources"
    if not sdir.is_dir():
        return set()
    return {p.stem for p in sdir.rglob("*") if p.is_file() and p.name != ".hashes.yaml"}


def _profile(bundle):
    purpose = (bundle / "purpose.md")
    purpose_text = purpose.read_text(encoding="utf-8", errors="replace") if purpose.is_file() else ""
    schema = (bundle / "SCHEMA.md")
    schema_text = schema.read_text(encoding="utf-8", errors="replace") if schema.is_file() else ""
    in_t, out_t = _scope(purpose_text)
    vocab = _tokens(purpose_text + " " + _schema_tags_text(schema_text) + " " + _concept_titles(bundle))
    goal = ""
    mg = re.search(r"#+\s*Goal\s*\n+(.+?)(?:\n#|\Z)", purpose_text, re.S | re.I)
    if mg:
        goal = " ".join(mg.group(1).split())[:140]
    return {
        "slug": bundle.name,
        "vocab": set(vocab) if vocab else set(),
        "scope_in": set(_tokens(in_t)),
        "scope_out": set(_tokens(out_t)),
        "prov": _source_stems(bundle),
        "goal": goal,
    }


def _discover(kb_root):
    return [d for d in sorted(kb_root.iterdir())
            if d.is_dir() and not d.name.startswith(".") and (d / "purpose.md").is_file()]


def _score(doc_w, doc_stem, prof):
    total = sum(doc_w.values()) or 1
    matched = {t: wt for t, wt in doc_w.items() if t in prof["vocab"]}
    vocab_cov = sum(matched.values()) / total
    in_t = prof["scope_in"] or set()
    out_t = prof["scope_out"] or set()
    in_cov = len(set(doc_w) & in_t) / (len(in_t) or 1)
    out_cov = len(set(doc_w) & out_t) / (len(out_t) or 1)
    prov = 0.1 if doc_stem in prof["prov"] else 0.0
    score = vocab_cov + 0.5 * in_cov - 0.5 * out_cov + prov
    sample = [t for t in sorted(matched, key=lambda t: (-matched[t], t))
              if not t.isdigit()][:12]
    return round(score, 4), sample


def main(argv=None):
    ap = argparse.ArgumentParser(description="Route a document to the right OKF bundle by content.")
    ap.add_argument("kb_root", type=Path)
    ap.add_argument("document", type=Path)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    kb_root = a.kb_root.expanduser().resolve()
    if not kb_root.is_dir():
        ap.error(f"not a directory: {kb_root}")
    if not a.document.is_file():
        ap.error(f"document not found: {a.document}")

    bundles = _discover(kb_root)
    if not bundles:
        print(f"no bundles (dirs with purpose.md) under {kb_root}", file=sys.stderr)
        return 2

    _, body = _frontmatter_and_body(a.document.read_text(encoding="utf-8", errors="replace"))
    doc_w = _tokens(body)
    doc_stem = a.document.stem

    ranked = []
    for b in bundles:
        score, sample = _score(doc_w, doc_stem, _profile(b))
        ranked.append({"bundle": b.name, "score": score, "matched_sample": sample})
    ranked.sort(key=lambda r: -r["score"])

    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    confident = (len(ranked) == 1) or (
        top["score"] >= MIN_SCORE and (top["score"] - second["score"]) >= MARGIN)
    decision = "route" if confident else "ambiguous"

    if a.json:
        print(json.dumps({
            "decision": decision, "confident": confident,
            "target": top["bundle"] if confident else None,
            "candidates": [r["bundle"] for r in ranked[:a.top]] if not confident else None,
            "ranked": ranked[:a.top],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"document: {a.document.name}\n")
        for r in ranked[:a.top]:
            mark = "→" if (confident and r is top) else " "
            print(f"  {mark} {r['bundle']:<24} score={r['score']:.3f}  "
                  f"[{', '.join(r['matched_sample'][:8])}]")
        print()
        if confident:
            print(f"✅ CONFIDENT → route to '{top['bundle']}' (auto-ingest, no need to ask).")
        else:
            cands = ", ".join(r["bundle"] for r in ranked[:a.top])
            print(f"❓ AMBIGUOUS (top two within {MARGIN}). Read purpose.md of [{cands}] "
                  f"and decide; ask the human only if still torn.")

    return 0 if confident else 3


if __name__ == "__main__":
    raise SystemExit(main())
