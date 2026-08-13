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

from aiwiki.engine.document import (
    OKFDocumentError,
    bundle_okf_version,
    concept_metadata,
    has_symlink_component,
    parse_document,
)
from aiwiki.engine.validate import validate_profile_document

_VIEWER_DIR = Path(__file__).resolve().parent.parent / "assets" / "viewer"
_INDEX_NAME = "index.md"
_RESERVED_NAMES = {"index.md", "log.md", "SCHEMA.md", "purpose.md"}
_SOURCES_DIR = "sources"
_PRIVATE_COMPONENTS = {".git"}
_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)(?:#[A-Za-z0-9_\-]*)?\)")

# Curated, harmonious hues tuned to read clearly against a deep-ink canvas.
# Types are assigned colors deterministically by sorted order so every bundle
# gets a legible, distinct-per-type palette (not a monochrome blob).
_PALETTE = [
    "#5eb3f6", "#5ee0c0", "#f6c453", "#f78c6b", "#c792ea", "#7ee787", "#f497b6",
    "#8aa0ff", "#ffd479", "#4fd6be", "#e3a7ff", "#9fd356", "#ff9e64", "#80cbc4",
]
_DEFAULT_NODE_COLOR = "#94a3b8"


def _palette_for(types: list[str]) -> dict[str, str]:
    """Assign one stable color per type, cycling the curated palette."""
    return {t: _PALETTE[i % len(_PALETTE)] for i, t in enumerate(sorted(types))}


def _parse_doc(text: str) -> tuple[dict[str, Any], str]:
    document = parse_document(text)
    return document.frontmatter, document.body


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
    trust: str = ""
    freshness: str = ""
    generated_by: str = ""
    generated_at: str = ""
    verified_by: list[str] = field(default_factory=list)
    verified_at: str = ""
    verification_current: bool = False
    current_verified_by: list[str] = field(default_factory=list)
    current_verified_at: str = ""
    stale_after: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
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
                "trust": self.trust,
                "freshness": self.freshness,
                "generated_by": self.generated_by,
                "generated_at": self.generated_at,
                "verified_by": self.verified_by,
                "verified_at": self.verified_at,
                "verification_current": self.verification_current,
                "current_verified_by": self.current_verified_by,
                "current_verified_at": self.current_verified_at,
                "stale_after": self.stale_after,
                "sources": self.sources,
                "size": 30 + min(60, len(self.body) // 200),
            }
        }


def _extract_links(body: str, doc_dir: Path, bundle_root: Path) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    bundle_root_resolved = bundle_root.resolve()
    for m in _LINK_RE.finditer(body):
        target = m.group(1)
        if "://" in target:
            continue
        try:
            base = bundle_root if target.startswith("/") else doc_dir
            candidate = base / target.lstrip("/")
            if has_symlink_component(bundle_root, candidate):
                continue
            resolved = candidate.resolve().relative_to(bundle_root_resolved)
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
        if has_symlink_component(bundle_root, md_path):
            continue
        rel_path = md_path.relative_to(bundle_root)
        # Skip the directory listing and raw source snapshots. The validator
        # already excludes sources/; raw sources are not curated concepts, so
        # graphing them only adds an orphan "Unknown" node.
        if (md_path.name in _RESERVED_NAMES
                or _SOURCES_DIR in rel_path.parts or ".okf" in rel_path.parts):
            continue
        if any(part.lower() in _PRIVATE_COMPONENTS for part in rel_path.parts):
            continue
        rel = rel_path.with_suffix("")
        concept_id = "/".join(rel.parts)
        try:
            fm, body = _parse_doc(md_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, OKFDocumentError):
            continue
        if validate_profile_document(fm, body):
            continue
        tags = fm.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        sources = fm.get("sources") or []
        if not isinstance(sources, list):
            sources = []
        meta = concept_metadata(fm)
        concepts.append(Concept(
            id=concept_id,
            type=str(fm.get("type") or "Unknown"),
            title=str(fm.get("title") or concept_id),
            description=str(fm.get("description") or ""),
            resource=str(fm.get("resource") or ""),
            tags=[str(t) for t in tags],
            body=body or "",
            status=meta["status"],
            trust=meta["trust"],
            freshness=meta["freshness"],
            generated_by=meta["generated_by"],
            generated_at=meta["generated_at"],
            verified_by=meta["verified_by"],
            verified_at=meta["verified_at"],
            verification_current=meta["verification_current"],
            current_verified_by=meta["current_verified_by"],
            current_verified_at=meta["current_verified_at"],
            stale_after=meta["stale_after"],
            sources=[source for source in sources if isinstance(source, dict)],
            links_to=_extract_links(body or "", md_path.parent, bundle_root),
        ))
    return concepts


def _build_graph(concepts: list[Concept], version: str = "") -> dict[str, Any]:
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
    trust_counts: dict[str, int] = {}
    freshness_counts: dict[str, int] = {}
    for c in concepts:
        trust_counts[c.trust] = trust_counts.get(c.trust, 0) + 1
        freshness_counts[c.freshness] = freshness_counts.get(c.freshness, 0) + 1
    return {"nodes": nodes, "edges": edges, "bodies": bodies,
            "types": types, "palette": palette, "counts": counts,
            "okf_version": version, "trust_counts": trust_counts,
            "freshness_counts": freshness_counts}


def generate_visualization(bundle_root: Path, out_path: Path,
                           bundle_name: str | None = None) -> dict[str, int]:
    """Walk a bundle and write a single self-contained HTML visualization."""
    bundle_root = Path(bundle_root)
    out_path = Path(out_path)
    if not bundle_root.is_dir():
        raise FileNotFoundError(f"Bundle directory not found: {bundle_root}")

    concepts = _walk_concepts(bundle_root)
    graph = _build_graph(concepts, bundle_okf_version(bundle_root))
    template = (_VIEWER_DIR / "viz.html").read_text(encoding="utf-8")
    css = (_VIEWER_DIR / "viz.css").read_text(encoding="utf-8")
    js = (_VIEWER_DIR / "viz.js").read_text(encoding="utf-8")
    name = bundle_name or bundle_root.resolve().name

    replacements = {
        "/*__VIZ_CSS__*/": css,
        "/*__VIZ_JS__*/": js,
        "__BUNDLE_NAME__": _script_json(name),
        "__BUNDLE_DATA__": _script_json(graph),
    }
    # Substitute only markers from the trusted template. Sequential ``replace``
    # calls can accidentally reinterpret marker-shaped text from bundle data.
    marker_re = re.compile("|".join(re.escape(marker) for marker in replacements))
    html = marker_re.sub(lambda match: replacements[match.group(0)], template)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return {"concepts": len(concepts), "edges": len(graph["edges"]),
            "bytes": len(html.encode("utf-8"))}


def _json_default(value: Any) -> str:
    """Serialize YAML date/datetime scalars embedded in structured sources."""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _script_json(value: Any) -> str:
    """Serialize JSON safely inside a classic HTML ``script`` element.

    JSON escaping alone does not protect ``</script>`` because the HTML parser
    terminates the element before JavaScript sees the string. Escaping the HTML
    delimiters plus JavaScript line separators keeps bundle-controlled content
    data-only without changing the decoded value.
    """
    serialized = json.dumps(value, ensure_ascii=False, default=_json_default)
    return (
        serialized
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


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
