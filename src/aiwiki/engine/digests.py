"""sha256 of a file, computed once per version of that file on disk.

The writer fingerprints the same unchanged files in every transaction: ~66 MB of immutable
``sources/`` evidence three to four times (source snapshot, drift check, hash ledger) and the
``.git`` object store (a 51 MB pack) once or twice, ~4 s of a changeset on the writer host
(2026-11-06). A digest is reused only while the file's ``(device, inode, size, mtime_ns,
ctime_ns)`` is unchanged. Any write changes ``ctime``, and no unprivileged process can set it
back, so a rewritten, replaced or truncated file is always hashed again; ``utime`` alone (which Git
does to freshen a pack it writes again) also changes ``ctime`` and only costs a rehash. A file that
changes while it is read is not cached.
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

_CACHE: dict[str, tuple[tuple[int, int, int, int, int], str]] = {}
_LOCK = threading.Lock()
_MAX = 65536


def _key(path: str) -> tuple[int, int, int, int, int]:
    st = os.stat(path, follow_symlinks=False)
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def file_sha256(path: Path | str) -> str:
    """Hex sha256 of ``path``'s bytes (``path`` itself, never a link's target)."""
    name = os.fspath(path)
    before = _key(name)
    with _LOCK:
        hit = _CACHE.get(name)
    if hit is not None and hit[0] == before:
        return hit[1]
    with open(name, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if _key(name) == before:
        with _LOCK:
            if len(_CACHE) >= _MAX:
                _CACHE.clear()
            _CACHE[name] = (before, digest)
    return digest
