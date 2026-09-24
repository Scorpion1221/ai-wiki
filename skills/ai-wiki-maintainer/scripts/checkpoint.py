#!/usr/bin/env python3
"""Compatibility shim: this script now lives in the ai-wiki package as ``aiwiki.maint.checkpoint``."""

import sys

from _aiwiki import load

if __name__ == "__main__":
    sys.exit(load("checkpoint", "checkpoint").main())
