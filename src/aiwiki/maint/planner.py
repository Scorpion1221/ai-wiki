"""Deterministic planning of maintainer queue items (design §4.4); no LLM and no I/O.

Collectors emit candidates ``{collector, topic_key, origin, brief, files, signals}``; ``plan``
turns them into items with a ``priority``. The writer keys each item (``item_key``) and serves
ready items with aging (``service.maint_state``), so neither is computed here. Noise rules,
evidence limits, redaction and chunking live here so every collector applies the same ones;
redaction uses ``runtime.secrets``, the rules the changeset gate scans evidence packets with.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any, TypeVar

from ..runtime import secrets

T = TypeVar("T")

PRIORITY = {
    "member": 100,  # member submissions (inbox)
    "memory": 80,  # shared memory and solution docs
    "task_doc": 70,  # a task root's README/status/PRD/report
    "issue_signal": 60,  # an issue with a decision or a status change
    "delta": 40,  # any other repository or issue increment
    "hygiene": 30,
    "refresh": 20,  # refresh and attention
}

ITEM_TEXT_LIMIT = 64 * 1024
ITEM_FILE_LIMIT = 64  # the writer refuses an item with more files (maint_state.MAX_FILES)
FILE_TEXT_LIMIT = 24 * 1024
BRIEF_LIMIT = 300

TASK_PREFIX = "tasks"
MEMORY_PREFIXES = ("memory", "docs/solutions")
TOPIC_PREFIXES = (TASK_PREFIX, *MEMORY_PREFIXES)
TASK_DOCS = frozenset({"readme", "status", "prd", "report"})

LOCKFILES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "poetry.lock",
    "uv.lock", "pipfile.lock", "cargo.lock", "gemfile.lock", "composer.lock", "go.sum", "pubspec.lock",
    "podfile.lock",
})
NOISE_DIRS = {
    "build": frozenset({"dist", "build", "out", ".next", ".nuxt", ".turbo", "coverage", "__pycache__"}),
    "vendored": frozenset({"vendor", "vendored", "node_modules", "bower_components", "third_party", "third-party"}),
    "test": frozenset({"test", "tests", "__tests__", "__mocks__", "__snapshots__", "e2e", "cypress"}),
    "ci": frozenset({".github", ".gitlab", ".circleci", ".buildkite", ".husky"}),
}
CI_FILES = frozenset({
    ".gitlab-ci.yml", ".travis.yml", "jenkinsfile", "azure-pipelines.yml", ".drone.yml", "bitbucket-pipelines.yml",
})
TEST_FILE = re.compile(r"^test_.+\.py$|_test\.(?:py|go)$|\.(?:test|spec)\.[cm]?[jt]sx?$")
MINIFIED = re.compile(r"\.min\.(?:js|css)$|\.map$")
# Credential stores: the secret rules only catch known token shapes, not ``DB_PASS=...``.
SECRET_FILE = re.compile(
    r"^\.env(?:\..+)?$|^\.netrc$|^id_(?:rsa|dsa|ecdsa|ed25519)|\.(?:pem|key|p12|pfx)$|^credentials.*\.json$"
)
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg", ".psd", ".pdf", ".doc", ".docx", ".xls",
    ".xlsx", ".ppt", ".pptx", ".zip", ".gz", ".tgz", ".tar", ".7z", ".rar", ".jar", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".mp3", ".mp4", ".mov", ".wav", ".webm", ".exe", ".dll", ".so", ".dylib", ".bin", ".wasm",
    ".pyc", ".class",
})


def noise(path: str) -> str | None:
    """The noise category of a changed path (counted, never frozen), or None."""
    parts = [part.lower() for part in PurePosixPath(path).parts]
    name = parts[-1]
    if SECRET_FILE.search(name):
        return "secret"
    if name in LOCKFILES or name.endswith(".lock"):
        return "lockfile"
    for kind, names in NOISE_DIRS.items():
        if names.intersection(parts[:-1]):
            return kind
    if name in CI_FILES:
        return "ci"
    if TEST_FILE.search(name):
        return "test"
    if MINIFIED.search(name):
        return "build"
    if PurePosixPath(name).suffix in BINARY_SUFFIXES:
        return "binary"
    return None


def topic_path(path: str, prefixes: Iterable[str] = TOPIC_PREFIXES) -> str:
    """``<prefix>/<entry>`` below a topic prefix (a task root), else the top directory ("." for root files)."""
    for prefix in sorted((prefix.strip("/") for prefix in prefixes), key=len, reverse=True):
        if prefix and path.startswith(prefix + "/"):
            return f"{prefix}/{path[len(prefix) + 1:].split('/', 1)[0]}"
    return path.split("/", 1)[0] if "/" in path else "."


def clip(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut to at most ``limit`` UTF-8 bytes, ending in a truncation marker; (text, truncated)."""
    data = text.encode()
    if len(data) <= limit:
        return text, False
    marker = f"\n… [truncated from {len(data)} bytes]\n"
    return data[:limit - len(marker.encode())].decode(errors="ignore") + marker, True


def evidence_file(name: str, text: str, origin: dict[str, Any], *, redactions: int = 0) -> dict[str, Any]:
    """A frozen evidence file: secrets redacted, clipped to ``FILE_TEXT_LIMIT``, hashed.

    ``redactions`` counts secrets the caller already redacted from ``text``.
    """
    redacted, count = secrets.redact(text)
    clipped, truncated = clip(redacted, FILE_TEXT_LIMIT)
    data = clipped.encode()
    return {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "data": data,
            "origin": {**origin, "truncated": truncated, "redactions": redactions + count}}


def utf8_size(block: str) -> int:
    return len(block.encode())


def pack(blocks: Iterable[T], limit: int, size: Callable[[T], int] = utf8_size) -> list[list[T]]:
    """Order-preserving greedy groups of blocks whose ``size`` (UTF-8 bytes) sums to at most ``limit``.

    A block larger than ``limit`` forms a group of its own; ``evidence_file`` then clips it.
    """
    groups: list[list[T]] = []
    total = 0
    for block in blocks:
        length = size(block)
        if not groups or total + length > limit:
            groups.append([])
            total = 0
        groups[-1].append(block)
        total += length
    return groups


def priority_class(candidate: dict[str, Any]) -> str:
    collector = candidate["collector"]
    signals = candidate.get("signals") or {}
    if collector == "inbox":
        return "member"
    if collector in ("hygiene", "refresh"):
        return collector
    if collector == "issues":
        return "issue_signal" if signals.get("decision") or signals.get("status_changes") else "delta"
    topic = candidate["topic_key"].partition("#")[2]
    if any(topic == prefix or topic.startswith(prefix + "/") for prefix in MEMORY_PREFIXES):
        return "memory"
    if topic.startswith(TASK_PREFIX + "/") and any(
        PurePosixPath(path).stem.lower() in TASK_DOCS for path in signals.get("paths", ())
    ):
        return "task_doc"
    return "delta"


def plan(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Queue items for ``POST /maint/items``, highest priority first; ``signals`` are consumed.

    Briefs are redacted too: they travel with the item, e.g. an issue title.
    """
    items = []
    for candidate in candidates:
        item = {key: value for key, value in candidate.items() if key != "signals"}
        brief = " ".join(secrets.redact(candidate.get("brief") or "")[0].split())
        item["brief"] = brief if len(brief) <= BRIEF_LIMIT else brief[:BRIEF_LIMIT - 1] + "…"
        item["priority"] = PRIORITY[priority_class(candidate)]
        items.append(item)
    return sorted(items, key=lambda item: (-item["priority"], item["topic_key"]))
