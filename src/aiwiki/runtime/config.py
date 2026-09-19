"""Optional, local writer-agent settings; credentials remain owned by Codex/wrappers."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_AGENT = {"bin": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high"}


def load_agent_config() -> dict[str, str]:
    """Resolve env > config.agent > defaults once when the worker starts.

    A missing default config preserves env-only deployments. An explicitly selected
    missing/invalid config fails closed rather than silently selecting another account.
    Never include configuration contents (which may contain tokens) in errors.
    """
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
    if agent.keys() - DEFAULT_AGENT.keys():
        raise ValueError("config.agent only supports bin, model, and reasoning_effort; keep credentials in the wrapper")

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
