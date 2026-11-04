"""Semantic recall for search: concept chunks embedded by an OpenAI-compatible endpoint.

Search stays lexical first. With ``AIWIKI_EMBED_URL``, ``AIWIKI_EMBED_MODEL`` and a key
(``AIWIKI_EMBED_KEY``) set, every concept is cut into chunks, each chunk is embedded once and
cached on disk by the sha256 of its exact input, and a query is embedded per request; a concept
scores by its best chunk. ``fuse`` merges that list with the lexical one (reciprocal rank fusion),
so a question asked in one language finds a concept written in another, while an exact phrase
keeps the first places. Nothing here ever blocks or fails a search: chunk vectors are built by a
background thread, and a query whose embedding is missing, slow or malformed is answered by the
lexical ranking alone, with the reason.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from array import array
from dataclasses import dataclass, field
from pathlib import Path

CHUNK_CHARS, CHUNK_OVERLAP = 1800, 200
RRF_K = 60
CANDIDATES = 100  # each list contributes its top 100 to the fusion
BATCH = 16  # chunks per embedding request while building
_CJK = re.compile(r"[一-鿿]")
_FOOTNOTE_DEF = re.compile(r"(?m)^\[\^[^\]]+\]:.*$")
_FRONTMATTER = re.compile(r"^---\n.*?\n---\n?", re.S)
try:
    _dot = math.sumprod  # 3.12+: about 16 ms over 2,656 x 768 float32
except AttributeError:  # 3.11
    import operator

    def _dot(a, b):
        return sum(map(operator.mul, a, b))


@dataclass(frozen=True)
class Settings:
    url: str
    model: str
    key: str
    dims: int
    timeout: float
    cache_dir: Path

    @property
    def tag(self) -> str:
        """Names the cache: a change of model or dimension never reuses another's vectors."""
        return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{self.model}-{self.dims}")


def settings(environ=os.environ) -> Settings | None:
    """The embedding endpoint from the environment, else None (search stays lexical)."""
    url, model, key = (environ.get(name, "").strip() for name in ("AIWIKI_EMBED_URL", "AIWIKI_EMBED_MODEL",
                                                                   "AIWIKI_EMBED_KEY"))
    if not (url and model and key):
        return None
    dims = int(environ.get("AIWIKI_EMBED_DIMS", "768"))
    timeout = float(environ.get("AIWIKI_EMBED_TIMEOUT_S", "3"))
    cache = Path(environ.get("AIWIKI_EMBED_CACHE", "/tmp/ai-wiki-embeddings")).expanduser()
    return Settings(url.rstrip("/"), model, key, dims, timeout, cache)


def chunks(title: str, description: str, body: str) -> list[str]:
    """A concept's embedding inputs: paragraph-packed chunks of at most CHUNK_CHARS, the first
    starting with the description; footnote definitions (provenance, not content) are dropped."""
    text = _FOOTNOTE_DEF.sub("", _FRONTMATTER.sub("", body))
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, cur = [], (description or "").strip()
    for para in paras:
        while len(para) > CHUNK_CHARS:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:CHUNK_CHARS])
            para = para[CHUNK_CHARS - CHUNK_OVERLAP:]
        if cur and len(cur) + len(para) + 2 > CHUNK_CHARS:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    head = f"title: {title or 'none'} | text: "
    return [head + c for c in (out or [title or "none"])]


def query_input(query: str) -> str:
    return f"task: search result | query: {query}"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unit(vec) -> array | None:
    """A unit-length float32 copy of ``vec`` (2,656 chunks x 768 take 7.8 MiB, not the 62 of floats)."""
    try:
        values = [float(x) for x in vec]
    except (TypeError, ValueError):
        return None
    norm = math.sqrt(_dot(values, values)) if values else 0.0
    if not norm or not math.isfinite(norm):
        return None
    return array("f", (x / norm for x in values))


def embed(config: Settings, inputs: list[str], timeout: float) -> list[array]:
    """Unit vectors for ``inputs`` from the endpoint, in order; raises on any failure or a
    malformed answer (wrong count, wrong dimension, non-numbers)."""
    body = json.dumps({"model": config.model, "input": inputs, "dimensions": config.dims}).encode()
    req = urllib.request.Request(f"{config.url}/v1/embeddings", data=body, method="POST", headers={
        "Authorization": f"Bearer {config.key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        answer = json.load(response)
    rows = sorted(answer.get("data") or [], key=lambda row: row.get("index", 0))
    vectors = [_unit(row.get("embedding")) for row in rows]
    if len(vectors) != len(inputs) or any(v is None or len(v) != config.dims for v in vectors):
        raise ValueError(f"the embedding endpoint answered {len(vectors)} vectors for {len(inputs)} inputs")
    return vectors


@dataclass
class _Index:
    """Chunk vectors of one snapshot's concepts, filled by the builder thread."""
    owners: list[str] = field(default_factory=list)  # the concept path of each chunk
    inputs: list[str] = field(default_factory=list)
    vectors: dict[int, array] = field(default_factory=dict)  # chunk position -> unit vector
    error: str | None = None


class Store:
    """The vector side of one bundle path: a disk cache shared by every snapshot, and the index of
    the newest snapshot it was asked about. Thread-safe; the builder runs at most once at a time."""

    def __init__(self, config: Settings, bundle: Path):
        self.config = config
        self.path = config.cache_dir / f"{_sha(str(bundle.resolve()))[:12]}-{config.tag}.jsonl"
        self.lock = threading.Lock()
        self.cache: dict[str, array] = {}
        self.index: _Index | None = None
        self.key: object = None  # the snapshot the index belongs to
        self.building = False
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        vec = _unit(row["v"])
                    except (ValueError, KeyError, TypeError):
                        continue  # a torn last line after a crash: that chunk is embedded again
                    if vec is not None and len(vec) == self.config.dims:
                        self.cache[row["s"]] = vec
        except FileNotFoundError:
            pass

    def _append(self, rows: list[tuple[str, array]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            for sha, vec in rows:
                f.write(json.dumps({"s": sha, "v": [round(x, 6) for x in vec]}) + "\n")

    def ensure(self, key: object, concepts: list[tuple[str, str, str, str]]) -> _Index:
        """The index for snapshot ``key`` (its (path, title, description, body) concepts), starting
        the builder for whatever the cache lacks. Never waits for an embedding."""
        with self.lock:
            if self.key is not key:
                index = _Index()
                for path, title, description, body in concepts:
                    for text in chunks(title, description, body):
                        index.owners.append(path)
                        index.inputs.append(text)
                for i, text in enumerate(index.inputs):
                    vec = self.cache.get(_sha(text))
                    if vec is not None:
                        index.vectors[i] = vec
                self.index, self.key = index, key
            index = self.index
            if len(index.vectors) < len(index.inputs) and not self.building:
                self.building = True
                threading.Thread(target=self._build, args=(index,), name="embedding-builder", daemon=True).start()
            return index

    def _build(self, index: _Index) -> None:
        try:
            todo = [i for i in range(len(index.inputs)) if i not in index.vectors]
            for start in range(0, len(todo), BATCH):
                part = todo[start:start + BATCH]
                texts = [index.inputs[i] for i in part]
                for attempt in range(5):
                    try:
                        vectors = embed(self.config, texts, timeout=max(30.0, self.config.timeout))
                        break
                    except (OSError, ValueError) as exc:
                        index.error = _why(exc)
                        time.sleep(min(60, 2 ** attempt))
                else:
                    return  # the next search after this snapshot's successor, or a restart, tries again
                rows = []
                with self.lock:
                    for i, vec in zip(part, vectors, strict=True):
                        index.vectors[i] = vec
                        sha = _sha(index.inputs[i])
                        self.cache[sha] = vec
                        rows.append((sha, vec))
                self._append(rows)
                index.error = None
        finally:
            with self.lock:
                self.building = False

    def state(self) -> dict:
        index = self.index
        total = len(index.inputs) if index else 0
        done = len(index.vectors) if index else 0
        return {"enabled": True, "model": self.config.model, "dims": self.config.dims,
                "chunks": total, "embedded": done, "ready": bool(total) and done == total,
                **({"error": index.error} if index and index.error else {})}

    def rank(self, index: _Index, query: str) -> tuple[list[str], str | None]:
        """Concept paths by their best chunk's cosine to ``query`` (top CANDIDATES), or ([], why)."""
        if not index.inputs or len(index.vectors) < len(index.inputs):
            return [], f"chunk vectors are being built ({len(index.vectors)}/{len(index.inputs)})"
        try:
            (qv,) = embed(self.config, [query_input(query)], timeout=self.config.timeout)
        except (OSError, ValueError) as exc:
            return [], f"query embedding failed: {_why(exc)}"
        best: dict[str, float] = {}
        for i, owner in enumerate(index.owners):
            sim = _dot(index.vectors[i], qv)
            if sim > best.get(owner, -2.0):
                best[owner] = sim
        return sorted(best, key=lambda p: (-best[p], p))[:CANDIDATES], None


def _why(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"status {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else exc
    return type(exc).__name__ if not str(exc) else f"{type(exc).__name__}: {str(exc)[:120]}"


def weights(query: str) -> tuple[float, float]:
    """Lexical:vector fusion weights: a query with Chinese in it matches the (mostly Chinese) text
    lexically as well as by meaning; one without leans on meaning."""
    return (1.0, 1.0) if _CJK.search(query) else (1.0, 3.0)


def fuse(lexical: list[str], phrase: set[str], vector: list[str], query: str) -> list[str]:
    """Reciprocal rank fusion of the two lists; concepts with an exact lexical phrase stay first,
    in their lexical order."""
    w_lex, w_vec = weights(query)
    score: dict[str, float] = {}
    for weight, ranked in ((w_lex, lexical[:CANDIDATES]), (w_vec, vector[:CANDIDATES])):
        for rank, path in enumerate(ranked):
            score[path] = score.get(path, 0.0) + weight / (RRF_K + rank + 1)
    first = [p for p in lexical if p in phrase]
    rest = sorted((p for p in score if p not in phrase), key=lambda p: (-score[p], p))
    return first + rest


_stores: dict[Path, Store] = {}
_stores_lock = threading.Lock()


def store(bundle: Path) -> Store | None:
    """The bundle's vector store when embedding is configured, else None."""
    config = settings()
    if config is None:
        return None
    with _stores_lock:
        current = _stores.get(bundle)
        if current is None or current.config != config:
            current = _stores[bundle] = Store(config, bundle)
        return current
