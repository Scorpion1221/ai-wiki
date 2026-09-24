"""Optional, local writer-agent settings; credentials remain owned by Codex/wrappers."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_AGENT = {"bin": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high"}
# Wall-clock budgets (seconds) for the curation pass, the adversarial audit and the
# bounded curation repair pass. Observed xhigh ingest p90 was ~675s against a fixed 900s.
DEFAULT_TIMEOUTS = {"timeout_s": 1500, "audit_timeout_s": 1200, "repair_timeout_s": 600}
TIMEOUT_RANGE = (60, 7200)


def _agent_section() -> dict:
    """Read and shape-check ``config.agent`` without exposing configuration contents."""
    selected = os.environ.get("AIWIKI_CONFIG")
    path = Path(selected or Path.home() / ".ai-wiki" / "config.json").expanduser()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if selected:
            raise ValueError(f"AIWIKI_CONFIG does not exist: {path}") from None
        config = {}
    except (OSError, ValueError):
        raise ValueError(f"cannot read valid JSON config from {path}") from None
    if not isinstance(config, dict):
        raise ValueError(f"config must contain a JSON object: {path}")
    agent = config.get("agent", {})
    if not isinstance(agent, dict):
        raise ValueError("config.agent must be an object")
    if agent.keys() - DEFAULT_AGENT.keys() - DEFAULT_TIMEOUTS.keys():
        raise ValueError(
            "config.agent only supports bin, model, reasoning_effort, timeout_s, audit_timeout_s, "
            "and repair_timeout_s; keep credentials in the wrapper"
        )
    return agent


def load_agent_config() -> dict[str, str]:
    """Resolve env > config.agent > defaults once when the worker starts.

    A missing default config preserves env-only deployments. An explicitly selected
    missing/invalid config fails closed rather than silently selecting another account.
    Never include configuration contents (which may contain tokens) in errors.
    """
    agent = _agent_section()
    resolved = {}
    for key, default in DEFAULT_AGENT.items():
        env = f"AIWIKI_AGENT_{key.upper()}"
        for source, value in ((f"config.agent.{key}", agent.get(key, default)),
                              (env, os.environ.get(env, agent.get(key, default)))):
            if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
                raise ValueError(f"{source} must be a non-empty string without control characters")
        resolved[key] = os.environ.get(env, agent.get(key, default))
    resolved["bin"] = os.path.expanduser(resolved["bin"])
    return resolved


def load_agent_timeouts() -> dict[str, int]:
    """Resolve env > config.agent > defaults for the agent wall-clock budgets.

    Each value must be an integer number of seconds in ``TIMEOUT_RANGE``; anything else
    fails closed instead of silently running with an unintended budget.
    """
    agent = _agent_section()
    low, high = TIMEOUT_RANGE
    resolved = {}
    for key, default in DEFAULT_TIMEOUTS.items():
        env = f"AIWIKI_AGENT_{key.upper()}"
        value = agent.get(key, default)
        source = f"config.agent.{key}"
        if env in os.environ:
            source, raw = env, os.environ[env].strip()
            value = int(raw) if raw.isdigit() else None
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{source} must be an integer number of seconds from {low} to {high}")
        resolved[key] = value
    return resolved
