"""Hand freed heap memory back to the system after a large structure is replaced.

glibc keeps what a program frees inside its heaps for reuse. With MALLOC_ARENA_MAX=2 the read
mirror still grew 150 -> 290 MiB in the 12 h after 0.3.16 (2026-11-05): every bundle pull
builds a new snapshot and a new chunk-vector matrix, a little larger than the one it frees, so
the freed space fragments instead of being reused. A local repro of 40 pulls on the production
bundle went 145 -> 230 MiB RSS; with ``trim()`` after each replacement it stayed at 140-161.
``malloc_trim(0)`` releases the free pages; where it does not exist (macOS, musl) this is a no-op,
and with jemalloc preloaded (the writer) glibc's heaps hold almost nothing to release.
"""
from __future__ import annotations

import ctypes

try:
    _malloc_trim = ctypes.CDLL(None).malloc_trim
except (AttributeError, OSError):
    _malloc_trim = None


def trim() -> None:
    if _malloc_trim is not None:
        _malloc_trim(0)
