"""Read-side bundle helpers for the service: listing, cat, grep, lexical search.

Deterministic, PyYAML + stdlib only — the same discipline as the engine. Path access
is sandboxed to the bundle root (safe-path gate).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

DELIM = "---"
RESERVED = {"index.md", "log.md", "SCHEMA.md", "purpose.md"}
SKIP_TOP = {"sources", ".okf"}
_CJK = r"一-鿿"

# A directory is a *bundle* (knowledge base) if it carries one of these markers. Used to
# discover the bundles hosted under a server's root dir, and to scaffold a new empty one.
_BUNDLE_MARKERS = ("SCHEMA.md", "purpose.md", "index.md", ".okf")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")  # bundle names: slug-ish, filesystem-safe


def is_bundle(p: Path) -> bool:
    return p.is_dir() and not p.name.startswith(".") and any((p / m).exists() for m in _BUNDLE_MARKERS)


def discover(root: Path) -> dict[str, Path]:
    """Map bundle-name -> path for every bundle directly under `root` (name-sorted)."""
    if not root.is_dir():
        return {}
    return {p.name: p for p in sorted(root.iterdir()) if is_bundle(p)}


def count_concepts(root: Path) -> int:
    return sum(1 for _ in concepts(root))


def scaffold(target: Path, name: str) -> None:
    """Create a minimal, valid empty bundle at `target` (ingest then populates it)."""
    (target / ".okf" / "jobs").mkdir(parents=True, exist_ok=True)
    (target / "sources" / "inbox").mkdir(parents=True, exist_ok=True)
    (target / "purpose.md").write_text(
        f"# Purpose\n\nKnowledge base **{name}** — newly created. Ingest sources to populate it.\n",
        encoding="utf-8")
    (target / "index.md").write_text(f"# {name}\n\n(empty — no concepts yet)\n", encoding="utf-8")
    (target / "index-meta.yaml").write_text(
        f"title: {name}\ndescription: \"\"\ndirectories: {{}}\n", encoding="utf-8")
    (target / "log.md").write_text("# Change log\n", encoding="utf-8")

# Structural (non-concept) markdown files, with what each is.
_DOC_LABELS = {
    "SCHEMA.md": "structural contract — read first (taxonomy, conventions, update policy)",
    "purpose.md": "why this KB exists — read first",
    "log.md": "change log",
    "index.md": "directory listing",
}


def parse(text: str) -> tuple[dict, str]:
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


def safe_resolve(root: Path, rel: str) -> Path:
    """Resolve rel under root, raising ValueError if it escapes (safe-path gate)."""
    p = (root / rel).resolve()
    p.relative_to(root.resolve())
    return p


def concepts(root: Path):
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if p.name in RESERVED or p.name.startswith("log-") or rel.parts[0] in SKIP_TOP:
            continue
        yield p, rel.as_posix()


def _in_dir(rel: str, subdir: str | None) -> bool:
    return not subdir or rel.startswith(subdir.strip("/") + "/")


def _count_concepts(root: Path, d: Path) -> int:
    n = 0
    for p in d.rglob("*.md"):
        rel = p.relative_to(root)
        if p.name in RESERVED or p.name.startswith("log-") or rel.parts[0] in SKIP_TOP:
            continue
        n += 1
    return n


def _dir_descriptions(root: Path) -> dict:
    meta = root / "index-meta.yaml"
    if not meta.is_file():
        return {}
    try:
        data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    dirs = data.get("directories") if isinstance(data, dict) else None
    return dirs if isinstance(dirs, dict) else {}


def _concept_entry(rel: str, fm: dict) -> dict:
    return {
        "kind": "concept", "path": rel,
        "title": fm.get("title"), "type": fm.get("type"),
        "status": fm.get("status"), "confidence": fm.get("confidence"),
        "timestamp": str(fm.get("timestamp") or ""),
        "source_updated_at": str(fm.get("source_updated_at") or ""),
        "tags": fm.get("tags") or [], "description": fm.get("description"),
    }


def _entry(root: Path, p: Path, descriptions: dict) -> dict:
    """Annotated listing entry for any path — dir / structural doc / concept / file."""
    rel = p.relative_to(root).as_posix()
    if p.is_dir():
        desc = descriptions.get(rel)
        if not desc and p.name == "sources":
            desc = "raw source snapshots (provenance)"
        return {"kind": "dir", "path": rel + "/", "name": p.name,
                "concepts": _count_concepts(root, p), "description": desc}
    if p.suffix == ".md" and (p.name in RESERVED or p.name.startswith("log-")):
        return {"kind": "doc", "path": rel, "name": p.name,
                "description": _DOC_LABELS.get(p.name) or "rotated change log"}
    if p.suffix == ".md":
        return _concept_entry(rel, parse(p.read_text(encoding="utf-8"))[0])
    return {"kind": "file", "path": rel, "name": p.name, "bytes": p.stat().st_size}


def list_dir(root: Path, subdir: str | None = None, recursive: bool = False,
             show_all: bool = False) -> list[dict]:
    """Faithful `ls` of a path inside the bundle: ALL entries at that level, name-sorted.

    Aligns with shell `ls`: lists every entry (dirs + files), hides dotfiles unless
    show_all (`-a`), recurses with recursive (`-R`). Each entry is annotated by kind —
    dir (concept count + index-meta description), doc (SCHEMA/purpose/log/index),
    concept (frontmatter), or file (size) — but nothing is filtered out. `ls` is the
    structural view; search/grep/health are the concept-semantic view (those still
    exclude sources/ and .okf/).
    """
    base = root if not subdir else (root / subdir.strip("/"))
    descriptions = _dir_descriptions(root)
    if base.is_file():
        return [_entry(root, base, descriptions)]
    if not base.is_dir():
        return []

    def hidden(rel_parts) -> bool:
        return any(part.startswith(".") for part in rel_parts) and not show_all

    if recursive:
        return [_entry(root, p, descriptions) for p in sorted(base.rglob("*"))
                if p.is_file() and not hidden(p.relative_to(root).parts)]
    return [_entry(root, p, descriptions) for p in sorted(base.iterdir())
            if not hidden((p.name,))]


def grep(root: Path, pattern: str, subdir: str | None = None, fixed: bool = False) -> list[dict]:
    # fixed=True → literal search (paths like `](../x.md)` are full of regex metachars).
    # Invalid regex raises re.error here; the caller (service) maps it to HTTP 400.
    rx = re.compile(re.escape(pattern) if fixed else pattern, re.IGNORECASE)
    hits = []
    for p, rel in concepts(root):
        if not _in_dir(rel, subdir):
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line):
                hits.append({"path": rel, "line": n, "text": line.strip()})
    return hits


def _tokens(q: str) -> set[str]:
    toks = {w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) >= 2}
    for run in re.findall(rf"[{_CJK}]+", q):
        if len(run) == 1:
            toks.add(run)
        for i in range(len(run) - 1):
            toks.add(run[i:i + 2])  # CJK bigrams — whitespace tokenization fails on Chinese
    return toks


def search(root: Path, query: str, top_k: int = 10) -> list[dict]:
    """Lexical, CJK-aware. title*8 + tags*4 + body*1, status/recency as tie-breakers."""
    terms = _tokens(query)
    if not terms:
        return []
    rank = {"canonical": 3, "reviewed": 2, "draft": 1, "stale": 0}
    scored = []
    for p, rel in concepts(root):
        fm, body = parse(p.read_text(encoding="utf-8"))
        title = str(fm.get("title") or "").lower()
        tags = " ".join(map(str, fm.get("tags") or [])).lower()
        low = body.lower()
        score = sum(8 * title.count(t) + 4 * tags.count(t) + low.count(t) for t in terms)
        if score:
            ts = str(fm.get("source_updated_at") or fm.get("timestamp") or "")
            scored.append((score, rank.get(fm.get("status"), 0), ts, {
                "path": rel, "title": fm.get("title"), "type": fm.get("type"),
                "status": fm.get("status"), "timestamp": str(fm.get("timestamp") or ""),
                "source_updated_at": str(fm.get("source_updated_at") or ""),
                "tags": fm.get("tags") or [], "score": score,
            }))
    # score desc, then status-rank desc, then recency (source_updated_at/timestamp) desc
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return [d for *_, d in scored[:top_k]]
