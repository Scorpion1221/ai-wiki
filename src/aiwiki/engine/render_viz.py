#!/usr/bin/env python3
"""Render an OKF bundle into a single self-contained HTML graph viewer.

Self-contained: depends only on Python 3 + PyYAML. It walks every markdown
concept in the bundle, builds a {nodes, edges, bodies, palette} graph, and
bakes it into the viewer template under ../assets/viewer/. No network, no LLM.

Re-run this whenever the bundle changes — it is deterministic and instant.
The viewer itself loads cytoscape / marked / highlight.js from a CDN at view
time, so opening the output HTML needs internet (see SKILL.md for offline use).

Usage:
    python3 render_viz.py <bundle-dir> [out.html] [--name "Display Name"]

Examples:
    python3 render_viz.py ~/Downloads/okf-web-plugin-knowledge
    python3 render_viz.py ./my-bundle /tmp/view.html --name "Team KB"
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_VIEWER_DIR = Path(__file__).resolve().parent.parent / "assets" / "viewer"
_INDEX_NAME = "index.md"
_RESERVED_NAMES = {"index.md", "log.md", "SCHEMA.md", "purpose.md"}
_SOURCES_DIR = "sources"
_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)(?:#[A-Za-z0-9_\-]*)?\)")
_FRONTMATTER_DELIM = "---"

# Curated, harmonious hues tuned to read clearly against a deep-ink canvas.
# Types are assigned colors deterministically by sorted order so every bundle
# gets a legible, distinct-per-type palette (not a monochrome blob).
_PALETTE = [
    "#5eb3f6", "#5ee0c0", "#f6c453", "#f78c6b", "#c792ea", "#7ee787", "#f497b6",
    "#8aa0ff", "#ffd479", "#4fd6be", "#e3a7ff", "#9fd356", "#ff9e64", "#80cbc4",
]
_DEFAULT_NODE_COLOR = "#94a3b8"


class OKFDocumentError(ValueError):
    pass


def _palette_for(types: list[str]) -> dict[str, str]:
    """Assign one stable color per type, cycling the curated palette."""
    return {t: _PALETTE[i % len(_PALETTE)] for i, t in enumerate(sorted(types))}


def _parse_doc(text: str) -> tuple[dict[str, Any], str]:
    """Lenient OKF parse: no frontmatter -> empty frontmatter + full body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return {}, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIM:
            end_idx = i
            break
    if end_idx is None:
        raise OKFDocumentError("Unterminated YAML frontmatter block")
    try:
        fm = yaml.safe_load("\n".join(lines[1:end_idx])) or {}
    except yaml.YAMLError as e:
        raise OKFDocumentError(f"Invalid YAML in frontmatter: {e}") from e
    if not isinstance(fm, dict):
        raise OKFDocumentError("Frontmatter must be a YAML mapping")
    body = "\n".join(lines[end_idx + 1:])
    if body.startswith("\n"):
        body = body[1:]
    return fm, body


@dataclass
class Concept:
    id: str
    type: str
    title: str
    description: str
    resource: str
    tags: list[str]
    body: str
    status: str = ""
    confidence: str = ""
    source_ref: str = ""
    links_to: list[str] = field(default_factory=list)

    def to_node(self) -> dict[str, Any]:
        return {
            "data": {
                "id": self.id,
                "label": self.title or self.id,
                "type": self.type,
                "description": self.description,
                "resource": self.resource,
                "tags": self.tags,
                "status": self.status,
                "confidence": self.confidence,
                "source_ref": self.source_ref,
                "size": 30 + min(60, len(self.body) // 200),
            }
        }


def _extract_links(body: str, doc_dir: Path, bundle_root: Path) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    bundle_root_resolved = bundle_root.resolve()
    for m in _LINK_RE.finditer(body):
        target = m.group(1)
        if "://" in target or target.startswith("/"):
            continue
        try:
            resolved = (doc_dir / target).resolve().relative_to(bundle_root_resolved)
        except ValueError:
            continue
        rel = resolved.as_posix()
        if rel.endswith(".md"):
            rel = rel[:-3]
        if rel and rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def _walk_concepts(bundle_root: Path) -> list[Concept]:
    concepts: list[Concept] = []
    for md_path in sorted(bundle_root.rglob("*.md")):
        rel_path = md_path.relative_to(bundle_root)
        # Skip the directory listing and raw source snapshots. The validator
        # already excludes sources/; raw sources are not curated concepts, so
        # graphing them only adds an orphan "Unknown" node.
        if (md_path.name in _RESERVED_NAMES or md_path.name.startswith("log-")
                or _SOURCES_DIR in rel_path.parts or ".okf" in rel_path.parts):
            continue
        rel = rel_path.with_suffix("")
        concept_id = "/".join(rel.parts)
        try:
            fm, body = _parse_doc(md_path.read_text(encoding="utf-8"))
        except OKFDocumentError:
            continue
        tags = fm.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        concepts.append(Concept(
            id=concept_id,
            type=str(fm.get("type") or "Unknown"),
            title=str(fm.get("title") or concept_id),
            description=str(fm.get("description") or ""),
            resource=str(fm.get("resource") or ""),
            tags=[str(t) for t in tags],
            body=body or "",
            status=str(fm.get("status") or ""),
            confidence=str(fm.get("confidence") or ""),
            source_ref=str(fm.get("source_ref") or fm.get("source_path") or ""),
            links_to=_extract_links(body or "", md_path.parent, bundle_root),
        ))
    return concepts


def _build_graph(concepts: list[Concept]) -> dict[str, Any]:
    ids = {c.id for c in concepts}
    nodes = [c.to_node() for c in concepts]
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    degree: dict[str, int] = {c.id: 0 for c in concepts}
    for c in concepts:
        for target in c.links_to:
            if target == c.id or target not in ids:
                continue
            key = (c.id, target)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            degree[c.id] += 1
            degree[target] += 1
            edges.append({"data": {"id": f"{c.id}__{target}", "source": c.id, "target": target}})

    types = sorted({c.type for c in concepts})
    palette = _palette_for(types)
    counts: dict[str, int] = {t: 0 for t in types}
    for node in nodes:
        d = node["data"]
        d["color"] = palette.get(d["type"], _DEFAULT_NODE_COLOR)
        d["size"] = 26 + min(46, degree.get(d["id"], 0) * 7)
        counts[d["type"]] = counts.get(d["type"], 0) + 1

    bodies = {c.id: c.body for c in concepts}
    return {"nodes": nodes, "edges": edges, "bodies": bodies,
            "types": types, "palette": palette, "counts": counts}


def generate_visualization(bundle_root: Path, out_path: Path,
                           bundle_name: str | None = None) -> dict[str, int]:
    """Walk a bundle and write a single self-contained HTML visualization."""
    bundle_root = Path(bundle_root)
    out_path = Path(out_path)
    if not bundle_root.is_dir():
        raise FileNotFoundError(f"Bundle directory not found: {bundle_root}")

    concepts = _walk_concepts(bundle_root)
    graph = _build_graph(concepts)
    template = (_VIEWER_DIR / "viz.html").read_text(encoding="utf-8")
    css = (_VIEWER_DIR / "viz.css").read_text(encoding="utf-8")
    js = (_VIEWER_DIR / "viz.js").read_text(encoding="utf-8")
    name = bundle_name or bundle_root.resolve().name

    html = (template
            .replace("/*__VIZ_CSS__*/", css)
            .replace("/*__VIZ_JS__*/", js)
            .replace("__BUNDLE_NAME__", json.dumps(name))
            .replace("__BUNDLE_DATA__", json.dumps(graph, ensure_ascii=False)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return {"concepts": len(concepts), "edges": len(graph["edges"]),
            "bytes": len(html.encode("utf-8"))}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Render an OKF bundle into a self-contained HTML graph viewer.")
    p.add_argument("bundle", type=Path, help="Path to the bundle root directory.")
    p.add_argument("out", type=Path, nargs="?", default=None,
                   help="Output HTML path (default: <bundle>/viz.html).")
    p.add_argument("--name", default=None,
                   help="Display name (default: bundle directory name).")
    args = p.parse_args(argv)

    bundle = args.bundle.expanduser()
    out = (args.out.expanduser() if args.out else bundle / "viz.html")
    stats = generate_visualization(bundle, out, bundle_name=args.name)
    print(f"✓ {stats['concepts']} concepts · {stats['edges']} edges "
          f"· {stats['bytes'] // 1024} KB → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
