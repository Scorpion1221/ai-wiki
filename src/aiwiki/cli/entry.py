"""Lightweight console entry point.

Keep version probes out of the full command graph. Agent harnesses call these often.
"""
from __future__ import annotations

import sys

from aiwiki.version import VERSION

_VERSION_FLAGS = {"-v", "-V", "--version"}


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 1 and args[0] in _VERSION_FLAGS:
        print(VERSION)
        return 0

    from aiwiki.cli.main import main as cli_main

    return cli_main(args)
