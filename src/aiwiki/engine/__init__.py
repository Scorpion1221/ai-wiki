"""Deterministic OKF bundle maintenance engine.

Hard constraint for everything in this package: **PyYAML + stdlib only — no LLM at
runtime, no network, no API keys.** The curating agent (headless Claude) does the
prose and the judgment; these modules do all the bookkeeping (indexing, validation,
drift detection, invariant enforcement, logging). Karpathy's framing: human curates,
LLM maintains, and the tedious bookkeeping is exactly what a deterministic engine
should own.

Each module is runnable as a CLI:

    python -m aiwiki.engine.<name> --help

and exposes a `main(argv=None) -> int` for import/testing and console entrypoints
(see [project.scripts] in pyproject.toml).
"""
