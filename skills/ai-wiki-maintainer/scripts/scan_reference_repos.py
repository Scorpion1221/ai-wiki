#!/usr/bin/env python3
"""Compatibility shim: this script now lives in the ai-wiki package as ``aiwiki.maint.collect_repos``."""

import sys

from _aiwiki import load

if __name__ == "__main__":
    sys.exit(load("collect_repos", "scan_reference_repos").main())
