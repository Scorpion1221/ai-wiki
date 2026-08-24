"""ai-wiki — hosted, agent-maintained OKF knowledge service.

Subpackages:
  engine  — deterministic OKF bundle maintenance (PyYAML + stdlib only).
  runtime — sandboxed Codex curation and adversarial-audit driver.
  service — FastAPI HTTP API (planned).
  cli     — `ai-wiki` thin client (planned).

See docs/design.md for the full architecture.
"""

from aiwiki.version import VERSION

__version__ = VERSION
