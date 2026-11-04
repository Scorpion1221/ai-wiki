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
PAUSE_S = 30  # after a failed query embedding, searches stay lexical this long instead of each waiting
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
    key: str = field(repr=False)  # never in a log line or a traceback's repr
    dims: int = 768
    timeout: float = 3.0
    cache_dir: Path = Path("/tmp/ai-wiki-embeddings")

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
    try:
        dims = int(environ.get("AIWIKI_EMBED_DIMS", "768"))
        timeout = float(environ.get("AIWIKI_EMBED_TIMEOUT_S", "3"))
    except ValueError:
        return None  # a malformed setting keeps search lexical rather than failing every request
    if dims <= 0 or not (0 < timeout <= 60):
        return None
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


class EmbedError(Exception):
    """The endpoint failed, timed out or answered something that is not the vectors asked for."""


def _post(config: Settings, inputs: list[str], timeout: float) -> list[array]:
    body = json.dumps({"model": config.model, "input": inputs, "dimensions": config.dims}).encode()
    req = urllib.request.Request(f"{config.url}/v1/embeddings", data=body, method="POST", headers={
        "Authorization": f"Bearer {config.key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        answer = json.loads(response.read())
    rows = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise EmbedError("the embedding endpoint answered no data list")
    order = [row.get("index") for row in rows]
    if sorted(i for i in order if type(i) is int) != list(range(len(inputs))):  # each input exactly once
        raise EmbedError("the embedding endpoint answered vectors without a usable index")
    vectors = [_unit(row.get("embedding")) for row in sorted(rows, key=lambda row: row["index"])]
    if len(vectors) != len(inputs) or any(v is None or len(v) != config.dims for v in vectors):
        raise EmbedError(f"the embedding endpoint answered {len(vectors)} vectors for {len(inputs)} inputs")
    return vectors


def embed(config: Settings, inputs: list[str], timeout: float) -> list[array]:
    """Unit vectors for ``inputs`` from the endpoint, in order, within ``timeout`` seconds in all
    (DNS, connection and a slowly trickling answer included); any failure or malformed answer
    raises EmbedError, never anything else. A request that outlives the deadline finishes on its
    own daemon thread and is discarded."""
    result: dict = {}

    def call():
        try:
            result["vectors"] = _post(config, inputs, timeout)
        except BaseException as exc:  # noqa: BLE001 - every failure is the caller's EmbedError
            result["error"] = exc

    worker = threading.Thread(target=call, name="embedding-request", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise EmbedError(f"no answer within {timeout:g}s")
    if "error" in result:
        exc = result["error"]
        raise EmbedError(_why(exc)) from (exc if isinstance(exc, Exception) else None)
    return result["vectors"]


@dataclass
class _Index:
    """Chunk vectors of one snapshot's concepts, filled by the builder thread."""
    owners: list[str] = field(default_factory=list)  # the concept path of each chunk
    shas: list[str] = field(default_factory=list)  # sha256 of each chunk's embedding input
    pending: dict[int, str] = field(default_factory=dict)  # chunk position -> input, until embedded
    vectors: dict[int, array] = field(default_factory=dict)  # chunk position -> unit vector
    error: str | None = None


class Store:
    """The vector side of one bundle path: a disk cache shared by every snapshot, and the index of
    the newest snapshot it was asked about. Thread-safe; one builder at a time, always working on
    the newest index, so a pull during a build never embeds a chunk twice.

    The cache file holds exactly the current index's vectors once a build completes (it is
    rewritten atomically, so it never grows with history); an unwritable cache directory only
    means the vectors live in memory until the next restart."""

    def __init__(self, config: Settings, bundle: Path):
        self.config = config
        self.path = config.cache_dir / f"{_sha(str(bundle.resolve()))[:12]}-{config.tag}.jsonl"
        self.lock = threading.Lock()
        self.cache: dict[str, array] = {}
        self.index: _Index | None = None
        self.key: object = None  # the snapshot the index belongs to
        self.building = False
        self.disk_error: str | None = None
        self.paused_until = 0.0  # monotonic time until which query embedding is skipped
        self.paused_why = ""
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return
        complete, _sep, _torn = raw.rpartition(b"\n")  # a torn last line after a crash is dropped
        for line in complete.decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
                vec = _unit(row["v"])
                sha = row["s"]
            except (ValueError, KeyError, TypeError):
                continue
            if vec is not None and len(vec) == self.config.dims and isinstance(sha, str):
                self.cache[sha] = vec

    def _append(self, rows: list[tuple[str, array]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("".join(json.dumps({"s": sha, "v": [round(x, 6) for x in vec]}) + "\n"
                                for sha, vec in rows))
            self.disk_error = None
        except OSError as exc:
            self.disk_error = _why(exc)

    def _compact(self, index: _Index) -> None:
        """Keep only ``index``'s vectors, on disk and in memory: chunks a pull replaced are dropped."""
        with self.lock:
            if self.index is not index:
                return
            keep = {sha: self.cache[sha] for sha in index.shas if sha in self.cache}
            self.cache = keep
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for sha, vec in keep.items():
                    f.write(json.dumps({"s": sha, "v": [round(x, 6) for x in vec]}) + "\n")
            os.replace(tmp, self.path)
            self.disk_error = None
        except OSError as exc:
            self.disk_error = _why(exc)

    def ensure(self, key: object, concepts) -> _Index:
        """The index for snapshot ``key`` (its (path, title, description, body) concepts), starting
        the builder for whatever the cache lacks. Never waits for an embedding."""
        with self.lock:
            if self.key is not key:
                index = _Index()
                for path, title, description, body in concepts:
                    for text in chunks(title, description, body):
                        i = len(index.owners)
                        sha = _sha(text)
                        index.owners.append(path)
                        index.shas.append(sha)
                        vec = self.cache.get(sha)
                        if vec is not None:
                            index.vectors[i] = vec
                        else:
                            index.pending[i] = text
                self.index, self.key = index, key
            index = self.index
            if index.pending and not self.building:
                self.building = True
                threading.Thread(target=self._build, name="embedding-builder", daemon=True).start()
            return index

    def _next_batch(self) -> tuple[_Index, list[int], list[str]] | None:
        """Up to BATCH chunks the newest index still lacks; ones the cache already holds (embedded
        for an older index) are filled in without a request."""
        with self.lock:
            index = self.index
            if index is None:
                return None
            for i in [i for i in index.pending if index.shas[i] in self.cache]:
                index.vectors[i] = self.cache[index.shas[i]]
                del index.pending[i]
            part = list(index.pending)[:BATCH]
            return (index, part, [index.pending[i] for i in part]) if part else None

    def _build(self) -> None:
        failures = 0
        done_for = None
        try:
            while True:
                batch = self._next_batch()
                if batch is None:
                    with self.lock:
                        done_for = self.index
                    break
                index, part, texts = batch
                try:
                    vectors = embed(self.config, texts, timeout=max(30.0, self.config.timeout))
                except EmbedError as exc:
                    index.error = str(exc)
                    failures += 1
                    if failures >= 5:
                        return  # the next search, or a restart, tries again
                    time.sleep(min(60, 2 ** failures))
                    continue
                failures = 0
                rows = []
                with self.lock:
                    for i, vec in zip(part, vectors, strict=True):
                        sha = index.shas[i]
                        self.cache[sha] = vec
                        rows.append((sha, vec))
                        if i in index.pending:
                            index.vectors[i] = vec  # before leaving pending: rank reads vectors once pending is empty
                            del index.pending[i]
                self._append(rows)
                index.error = None
        finally:
            with self.lock:
                self.building = False
            if done_for is not None and not done_for.pending:
                self._compact(done_for)

    def state(self) -> dict:
        index = self.index
        total = len(index.owners) if index else 0
        done = len(index.vectors) if index else 0
        error = (index.error if index else None) or self.disk_error
        return {"enabled": True, "model": self.config.model, "dims": self.config.dims,
                "chunks": total, "embedded": done, "ready": bool(total) and done == total,
                **({"error": error} if error else {})}

    def rank(self, index: _Index, query: str) -> tuple[list[str], str | None]:
        """Concept paths by their best chunk's cosine to ``query`` (top CANDIDATES), or ([], why)."""
        if not index.owners or index.pending:
            return [], f"chunk vectors are being built ({len(index.vectors)}/{len(index.owners)})"
        left = self.paused_until - time.monotonic()
        if left > 0:
            return [], f"query embedding paused for {left:.0f}s after: {self.paused_why}"
        try:
            (qv,) = embed(self.config, [query_input(query)], timeout=self.config.timeout)
        except EmbedError as exc:
            self.paused_why, self.paused_until = str(exc), time.monotonic() + PAUSE_S
            return [], f"query embedding failed: {exc}"
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
