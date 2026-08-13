"""Read-side bundle helpers for the service: listing, cat, grep, lexical search.

Deterministic, PyYAML + stdlib only — the same discipline as the engine. Path access
is sandboxed to the bundle root (safe-path gate).
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

import yaml

from aiwiki.engine.document import (
    FRESHNESS_STALE,
    TRUST_HUMAN_REVIEWED,
    TRUST_MACHINE_CONFIRMED,
    OKFDocumentError,
    bundle_okf_version,
    concept_metadata,
    has_symlink_component,
    parse_document,
)
from aiwiki.engine.validate import OKF_VERSION, validate_profile_document

RESERVED = {"index.md", "log.md", "SCHEMA.md", "purpose.md"}
SKIP_TOP = {"sources", ".okf"}
PRIVATE_COMPONENTS = {".git"}
_CJK = r"一-鿿"

# A directory is a *bundle* (knowledge base) if it carries one of these markers. Used to
# discover the bundles hosted under a server's root dir, and to scaffold a new empty one.
_BUNDLE_MARKERS = ("SCHEMA.md", "purpose.md", "index.md", ".okf")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")  # bundle names: slug-ish, filesystem-safe


def _bundle_is_v02(root: Path) -> bool:
    """Read surfaces expose concepts only from an explicitly versioned v0.2 bundle."""
    return bundle_okf_version(root) == OKF_VERSION


def _candidate_rel(root: Path, path: Path) -> str | None:
    """Return a safe concept-relative path, rejecting structural/source/escaped files."""
    if has_symlink_component(root, path):
        return None
    try:
        rel = path.relative_to(root)
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if (
        path.suffix != ".md"
        or path.name in RESERVED
        or not rel.parts
        or any(part.lower() in PRIVATE_COMPONENTS for part in rel.parts)
        or rel.parts[0] in SKIP_TOP
    ):
        return None
    return rel.as_posix()


def _profile_document(root: Path, path: Path) -> tuple[dict, str, str] | None:
    """Parse one safe concept and fail closed on every strict-profile error."""
    if _candidate_rel(root, path) is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
        frontmatter, body = parse(text)
    except (OSError, UnicodeError, OKFDocumentError):
        return None
    if validate_profile_document(frontmatter, body):
        return None
    return frontmatter, body, text


def _concept_records(root: Path):
    """Yield each valid v0.2 concept once with parsed and original content."""
    if not _bundle_is_v02(root):
        return
    for path in sorted(root.rglob("*.md")):
        rel = _candidate_rel(root, path)
        if rel is None:
            continue
        document = _profile_document(root, path)
        if document is not None:
            yield path, rel, *document


def is_bundle(p: Path) -> bool:
    return (
        p.is_dir()
        and not p.is_symlink()
        and not p.name.startswith(".")
        and any((p / m).exists() and not (p / m).is_symlink() for m in _BUNDLE_MARKERS)
    )


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
    # .okf/ and sources/inbox/ are operational state — only curated source snapshots
    # under sources/ belong in Git.
    (target / ".gitignore").write_text(
        ".okf/\nsources/inbox/\nviz.html\n.obsidian/\n.gstack/\n.DS_Store\n",
        encoding="utf-8",
    )
    (target / "purpose.md").write_text(
        f"---\ntype: Contract\n---\n\n# Purpose\n\n"
        f"Knowledge base **{name}** — newly created. Ingest sources to populate it.\n",
        encoding="utf-8")
    (target / "SCHEMA.md").write_text(
        "---\ntype: Contract\n---\n\n# Schema\n\n"
        "This bundle uses OKF v0.2 and the AI Wiki strict profile.\n",
        encoding="utf-8",
    )
    (target / "index.md").write_text(
        f'---\nokf_version: "0.2"\n---\n\n# {name}\n\n(empty — no concepts yet)\n',
        encoding="utf-8",
    )
    (target / "index-meta.yaml").write_text(
        f"title: {name}\ndescription: \"\"\ndirectories: {{}}\n", encoding="utf-8")
    (target / "log.md").write_text("# Change log\n", encoding="utf-8")


def commit_scaffold(target: Path, name: str) -> dict:
    """Make a newly-created bundle immediately writable by the Git-backed worker.

    Every service-created bundle owns its repository. Sharing an enclosing repository
    would make a failed transaction's reset/clean capable of touching sibling bundles.
    This trusted deterministic output is committed before any ingest job is queued.
    """
    try:
        enclosing = subprocess.run(
            ["git", "-C", str(target.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git is unavailable: {exc}") from exc

    try:
        subprocess.run(["git", "init", "-q", "-b", "main", str(target)], check=True, timeout=10)
        subprocess.run(
            ["git", "-C", str(target), "config", "user.name", "AI Wiki Worker"],
            check=True, timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(target), "config", "user.email", "ai-wiki@localhost"],
            check=True, timeout=10,
        )
        subprocess.run(["git", "-C", str(target), "add", "-A"], check=True, timeout=10)
        commit = subprocess.run(
            ["git", "-C", str(target), "commit", "-m", f"bundle: create {name}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"failed to initialize bundle repository: {exc}") from exc
    if commit.returncode != 0:
        raise RuntimeError((commit.stdout + commit.stderr).strip() or "bundle commit failed")
    # A bundles root may itself be a repository. Keep the independent nested repo out
    # of its operational status without creating a tracked parent-repository change.
    if enclosing.returncode == 0 and enclosing.stdout.strip():
        parent_root = Path(enclosing.stdout.strip()).resolve()
        try:
            rel = target.resolve().relative_to(parent_root).as_posix()
        except ValueError:
            pass
        else:
            git_path = subprocess.run(
                ["git", "-C", str(parent_root), "rev-parse", "--git-path", "info/exclude"],
                capture_output=True, text=True, timeout=10,
            )
            if git_path.returncode == 0 and git_path.stdout.strip():
                exclude = Path(git_path.stdout.strip())
                if not exclude.is_absolute():
                    exclude = parent_root / exclude
                pattern = f"/{rel}/"
                existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
                if pattern not in existing.splitlines():
                    exclude.parent.mkdir(parents=True, exist_ok=True)
                    with exclude.open("a", encoding="utf-8") as f:
                        if existing and not existing.endswith("\n"):
                            f.write("\n")
                        f.write(pattern + "\n")
    return {"repository": str(target), "commit": git_revision(target), "initialized": True}

# Structural (non-concept) markdown files, with what each is.
_DOC_LABELS = {
    "SCHEMA.md": "structural contract — read first (taxonomy, conventions, update policy)",
    "purpose.md": "why this KB exists — read first",
    "log.md": "change log",
    "index.md": "directory listing",
}


def parse(text: str) -> tuple[dict, str]:
    document = parse_document(text)
    return document.frontmatter, document.body


def git_revision(root: Path) -> str | None:
    """Return the repository HEAD containing the bundle, when one exists."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision or None


def health(root: Path) -> dict:
    """Bundle health summary with OKF v0.2 trust/freshness signals."""
    types: Counter = Counter()
    statuses: Counter = Counter()
    trusts: Counter = Counter()
    freshness_counts: Counter = Counter()
    total = 0
    for _path, _rel, fm, _body, _text in _concept_records(root):
        meta = concept_metadata(fm)
        total += 1
        types[str(fm.get("type") or "")] += 1
        statuses[meta["status"]] += 1
        trusts[meta["trust"]] += 1
        freshness_counts[meta["freshness"]] += 1
    return {
        "okf_version": bundle_okf_version(root),
        "git_revision": git_revision(root),
        "concepts": total,
        "by_type": dict(types),
        "by_status": dict(statuses),
        "by_trust": dict(trusts),
        "by_freshness": dict(freshness_counts),
    }


def safe_resolve(root: Path, rel: str) -> Path:
    """Resolve rel under root, raising ValueError if it escapes (safe-path gate)."""
    lexical = Path(rel)
    if lexical.is_absolute():
        raise ValueError("absolute paths are not bundle-relative")
    if any(part.lower() in PRIVATE_COMPONENTS for part in lexical.parts):
        raise ValueError("private bundle path")
    candidate = root / rel
    if has_symlink_component(root, candidate):
        raise ValueError("path traverses a symlink")
    p = candidate.resolve()
    p.relative_to(root.resolve())
    return p


def concepts(root: Path):
    for path, rel, _frontmatter, _body, _text in _concept_records(root):
        yield path, rel


def _in_dir(rel: str, subdir: str | None) -> bool:
    return not subdir or rel.startswith(subdir.strip("/") + "/")


def _count_concepts(root: Path, d: Path, *, bundle_v02: bool | None = None) -> int:
    if bundle_v02 is None:
        bundle_v02 = _bundle_is_v02(root)
    if not bundle_v02:
        return 0
    n = 0
    for p in d.rglob("*.md"):
        if _profile_document(root, p) is not None:
            n += 1
    return n


def _dir_descriptions(root: Path) -> dict:
    meta = root / "index-meta.yaml"
    if has_symlink_component(root, meta) or not meta.is_file():
        return {}
    try:
        data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    dirs = data.get("directories") if isinstance(data, dict) else None
    return dirs if isinstance(dirs, dict) else {}


def _concept_entry(rel: str, fm: dict) -> dict:
    return {
        "kind": "concept", "path": rel,
        "title": fm.get("title"), "type": fm.get("type"),
        "tags": fm.get("tags") or [], "description": fm.get("description"),
        **concept_metadata(fm),
    }


def metadata_for_path(root: Path, path: Path) -> dict | None:
    """Derived metadata for one valid concept, or ``None`` when it is not consumable."""
    if not _bundle_is_v02(root):
        return None
    document = _profile_document(root, path)
    return concept_metadata(document[0]) if document is not None else None


def _entry(root: Path, p: Path, descriptions: dict, *, bundle_v02: bool | None = None) -> dict:
    """Annotated listing entry for any path — dir / structural doc / concept / file."""
    rel = p.relative_to(root).as_posix()
    if bundle_v02 is None:
        bundle_v02 = _bundle_is_v02(root)
    if p.is_dir():
        desc = descriptions.get(rel)
        if not desc and p.name == "sources":
            desc = "raw source snapshots (provenance)"
        return {"kind": "dir", "path": rel + "/", "name": p.name,
                "concepts": _count_concepts(root, p, bundle_v02=bundle_v02), "description": desc}
    if p.suffix == ".md" and p.name in RESERVED:
        return {"kind": "doc", "path": rel, "name": p.name,
                "description": _DOC_LABELS.get(p.name) or "rotated change log"}
    if p.suffix == ".md":
        document = _profile_document(root, p) if bundle_v02 else None
        if document is None:
            reason = "invalid strict OKF v0.2 concept" if bundle_v02 else "bundle is not OKF v0.2"
            return {"kind": "invalid", "path": rel, "name": p.name, "description": reason}
        fm, _body, _text = document
        return _concept_entry(rel, fm)
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
    if subdir and Path(subdir).is_absolute():
        raise ValueError("absolute paths are not bundle-relative")
    base = root if not subdir else safe_resolve(root, subdir)
    bundle_v02 = _bundle_is_v02(root)
    descriptions = _dir_descriptions(root)
    if base.is_file():
        return [_entry(root, base, descriptions, bundle_v02=bundle_v02)]
    if not base.is_dir():
        return []

    def hidden(rel_parts) -> bool:
        return any(part.startswith(".") for part in rel_parts) and not show_all

    def visible_safe(path: Path, parts) -> bool:
        if any(part.lower() in PRIVATE_COMPONENTS for part in parts):
            return False
        if hidden(parts):
            return False
        if has_symlink_component(root, path):
            return False
        try:
            path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return False
        return True

    if recursive:
        return [
            _entry(root, p, descriptions, bundle_v02=bundle_v02)
            for p in sorted(base.rglob("*"))
            if p.is_file() and visible_safe(p, p.relative_to(root).parts)
        ]
    return [
        _entry(root, p, descriptions, bundle_v02=bundle_v02)
        for p in sorted(base.iterdir())
        if visible_safe(p, (p.name,))
    ]


def grep(root: Path, pattern: str, subdir: str | None = None, fixed: bool = False) -> list[dict]:
    # fixed=True → literal search (paths like `](../x.md)` are full of regex metachars).
    # Invalid regex raises re.error here; the caller (service) maps it to HTTP 400.
    rx = re.compile(re.escape(pattern) if fixed else pattern, re.IGNORECASE)
    hits = []
    for _path, rel, _frontmatter, _body, text in _concept_records(root):
        if not _in_dir(rel, subdir):
            continue
        for n, line in enumerate(text.splitlines(), 1):
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


def _snippet(body: str, terms: set[str], width: int = 160) -> str:
    """Best-matching body line — lets the caller judge relevance without a cat."""
    best, best_hits = "", 0
    for line in body.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if not stripped:
            continue
        low = stripped.lower()
        hits = sum(low.count(t) for t in terms)
        if hits > best_hits:
            best, best_hits = stripped, hits
    return best[:width]


def search(root: Path, query: str, top_k: int | None = 10) -> list[dict]:
    """Lexical, CJK-aware. (title|aliases)*8 + (tags|description)*4 + body*1,
    OKF trust/freshness as a tie-breaker. Results carry description + best-line snippet
    so the caller can prune candidates without cat-ing each one."""
    terms = _tokens(query)
    if not terms:
        return []
    scored = []
    for _path, rel, fm, body, _text in _concept_records(root):
        title = str(fm.get("title") or "").lower()
        aliases = " ".join(map(str, fm.get("aliases") or [])).lower()
        tags = " ".join(map(str, fm.get("tags") or [])).lower()
        desc = str(fm.get("description") or "").lower()
        low = body.lower()
        score = sum(8 * (title.count(t) + aliases.count(t))
                    + 4 * (tags.count(t) + desc.count(t))
                    + low.count(t) for t in terms)
        if score:
            meta = concept_metadata(fm)
            knowledge_rank = _knowledge_rank(meta)
            scored.append((score, knowledge_rank, meta["generated_at"], {
                "path": rel, "title": fm.get("title"), "type": fm.get("type"),
                "tags": fm.get("tags") or [], "score": score,
                "description": fm.get("description"),
                "snippet": _snippet(body, terms),
                **meta,
            }))
    # Relevance finds the requested concept; trust/freshness gate close matches.
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    results = [d for *_, d in scored]
    return results if top_k is None else results[:top_k]


def _knowledge_rank(meta: dict) -> int:
    """Fail-closed read priority requested by the AI Wiki consumer contract."""
    status, fresh, trust = meta["status"], meta["freshness"], meta["trust"]
    if status == "deprecated" or fresh == FRESHNESS_STALE:
        return 0
    if status == "draft":
        return 1
    if status == "stable" and fresh == "fresh" and trust == TRUST_HUMAN_REVIEWED:
        return 4
    if status == "stable" and fresh == "fresh" and trust == TRUST_MACHINE_CONFIRMED:
        return 3
    return 2 if status == "stable" else 1


_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)(?:#[^)]*)?\)")


def _outbound(root: Path, body: str, rel: str, ids: set[str]) -> list[str]:
    """Concept paths this concept's body links to (relative markdown links only)."""
    out, cdir = [], (root / rel).parent
    for m in _LINK_RE.finditer(body):
        target = m.group(1)
        if "://" in target:
            continue
        try:
            base = root if target.startswith("/") else cdir
            candidate = base / target.lstrip("/")
            if has_symlink_component(root, candidate):
                continue
            resolved = candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if resolved in ids and resolved != rel and resolved not in out:
            out.append(resolved)
    return out


def links(root: Path, rel: str) -> dict:
    """Both directions of the concept link graph for `rel`: outbound (what it
    references) and inbound (what references it — the backlinks `# Related concepts`
    alone cannot give, since concept links are forward-only)."""
    rel = rel.strip("/")
    target = safe_resolve(root, rel)  # ValueError (escape) handled by caller
    if not target.is_file():
        raise FileNotFoundError(rel)
    by_rel = {
        rel: (path, fm, body)
        for path, rel, fm, body, _text in _concept_records(root)
    }
    if rel not in by_rel:
        raise FileNotFoundError(rel)  # reserved/structural files have no link graph
    ids = set(by_rel)

    def entry(r: str) -> dict:
        _path, fm, _body = by_rel[r]
        return {"path": r, "title": fm.get("title"), "type": fm.get("type"),
                "description": fm.get("description")}

    _path, fm, body = by_rel[rel]
    outbound = _outbound(root, body, rel, ids)
    inbound = [r for r in sorted(ids)
               if r != rel and rel in _outbound(root, by_rel[r][2], r, ids)]
    return {"path": rel, "title": fm.get("title"),
            "outbound": [entry(r) for r in outbound],
            "inbound": [entry(r) for r in inbound]}
