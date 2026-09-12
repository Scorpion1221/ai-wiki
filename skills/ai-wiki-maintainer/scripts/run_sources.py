#!/usr/bin/env python3
"""Forward existing scheduled callers to the installed CLI's maintenance implementation."""
from __future__ import annotations

import argparse
import os
import sys

if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        os.execvp("ai-wiki", ["ai-wiki", "maintain", "--help"])
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--bundle", required=True)
    args, rest = parser.parse_known_args()
    os.execvp("ai-wiki", ["ai-wiki", "-b", args.bundle, "maintain", *rest, "--json"])
