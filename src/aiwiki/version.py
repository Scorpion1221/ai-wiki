"""Version leaf for cheap CLI probes."""
from __future__ import annotations

import os

VERSION = "0.2.9"

_BUILD: list[str | None] = []


def build() -> str | None:
    """The deployed Git revision of this process, or None when the deploy did not record it.

    Resolved once per process: env ``AIWIKI_BUILD_COMMIT``, else the first line of
    ``<app root>/.ai-wiki-deployed-revision`` (the checkout that contains ``src/``).
    """
    if not _BUILD:
        value = os.environ.get("AIWIKI_BUILD_COMMIT", "").strip()
        if not value:
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            try:
                with open(os.path.join(root, ".ai-wiki-deployed-revision"), encoding="utf-8") as marker:
                    value = marker.readline().strip()
            except (OSError, UnicodeDecodeError):
                value = ""
        _BUILD.append(value[:64] or None)
    return _BUILD[0]


def service_identity() -> dict:
    """Stamped on every writer job so a client can tell which deploy produced a receipt."""
    return {"version": VERSION, "build": build()}
