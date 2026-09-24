"""Import the ``aiwiki.maint`` module behind a compatibility shim in this directory.

The collection scripts moved into the ai-wiki package. A repository checkout's ``src`` wins,
so skill and code stay one revision; otherwise the package is imported from this interpreter.
A system ``python3`` without it re-executes the shim once with the installed ``ai-wiki``
CLI's interpreter, which is how a deployed skill runs.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType

REEXEC_ENV = "AIWIKI_MAINT_SHIM_REEXEC"
UPGRADE = "uv tool install --force git+https://github.com/Scorpion1221/ai-wiki && hash -r"


def load(module: str, label: str) -> ModuleType:
    src = Path(__file__).resolve().parents[3] / "src"
    if (src / "aiwiki" / "maint").is_dir():
        sys.path.insert(0, str(src))
    try:
        return importlib.import_module(f"aiwiki.maint.{module}")
    except ImportError as exc:
        error = exc
    cli = shutil.which("ai-wiki")
    python = Path(cli).resolve().with_name("python3") if cli else None
    if python and python.is_file() and not os.environ.get(REEXEC_ENV):
        os.environ[REEXEC_ENV] = "1"
        os.execv(python, [str(python), sys.argv[0], *sys.argv[1:]])
    print(f"{label}: fatal: cannot import aiwiki.maint.{module} ({error}); upgrade the ai-wiki CLI: {UPGRADE}",
          file=sys.stderr)
    raise SystemExit(2)
