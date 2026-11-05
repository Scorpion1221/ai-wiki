"""sha256 of a file, computed once per version of that file on disk.

The writer fingerprints the same unchanged files in every transaction: ~66 MB of immutable
``sources/`` evidence three to four times (source snapshot, drift check, hash ledger) and the
``.git`` object store (a 51 MB pack) once or twice, ~4 s of a changeset on the writer host
(2026-11-06). A digest is reused only while the file's ``(device, inode, size, mtime_ns,
ctime_ns)`` is unchanged. A write changes ``ctime``, and no unprivileged process can set it back,
so a rewritten, replaced or truncated file is hashed again. The limit is the clock tick that stamps
``ctime`` (1-10 ms on Linux, up to 1 s on some filesystems): a same-size rewrite of the same inode
within one tick of the cached stat would keep the old digest. Nothing on the writer does that:
sources are written once and never rewritten, Git never rewrites an object file in place, and the
gate's disposable copy under the temp directory (the only tree an agent could write) is never
cached. ``utime`` alone (Git freshening a pack) also moves ``ctime`` and only costs a rehash. A file
that changes while it is read is not cached.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

_CACHE: OrderedDict[str, tuple[tuple[int, int, int, int, int], str]] = OrderedDict()
_LOCK = threading.Lock()
_MAX = 32768  # entries (~15 MB); the least recently used go first
# Every changeset's gate hashes a fresh copy under the temp directory; those paths never come back.
_TEMP = os.path.join(os.path.realpath(tempfile.gettempdir()), "")


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
            _CACHE.move_to_end(name)
            return hit[1]
    with open(name, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if _key(name) == before and not os.path.realpath(name).startswith(_TEMP):
        with _LOCK:
            _CACHE[name] = (before, digest)
            _CACHE.move_to_end(name)
            while len(_CACHE) > _MAX:
                _CACHE.popitem(last=False)
    return digest
