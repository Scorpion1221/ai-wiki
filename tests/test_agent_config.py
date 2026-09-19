"""Writer selection is local, explicit, backward compatible, and credential-free."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiwiki.runtime.config import DEFAULT_AGENT, load_agent_config


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AIWIKI_CONFIG", raising=False)
    for key in DEFAULT_AGENT:
        monkeypatch.delenv(f"AIWIKI_AGENT_{key.upper()}", raising=False)
    path = tmp_path / ".ai-wiki" / "config.json"
    path.parent.mkdir()
    return path


@pytest.mark.parametrize("config", [None, {}, {"endpoint": "http://localhost", "token": "secret"}])
def test_missing_agent_keeps_subscription_defaults(config_path: Path, config) -> None:
    if config is not None:
        config_path.write_text(json.dumps(config))
    assert load_agent_config() == DEFAULT_AGENT


def test_wrapper_config_and_environment_precedence(config_path: Path, monkeypatch) -> None:
    config_path.write_text(json.dumps({"agent": {
        "bin": "~/.local/bin/codex-9router", "model": "gpt-6-astra-combos", "reasoning_effort": "xhigh",
    }}))
    expected = {
        "bin": str(config_path.parents[1] / ".local/bin/codex-9router"),
        "model": "gpt-6-astra-combos", "reasoning_effort": "xhigh",
    }
    assert load_agent_config() == expected
    monkeypatch.setenv("AIWIKI_AGENT_MODEL", "env-model")
    assert load_agent_config() == {**expected, "model": "env-model"}
    monkeypatch.setenv("AIWIKI_AGENT_BIN", "env-codex")
    monkeypatch.setenv("AIWIKI_AGENT_REASONING_EFFORT", "medium")
    assert load_agent_config() == {"bin": "env-codex", "model": "env-model", "reasoning_effort": "medium"}


def test_partial_config_and_explicit_path(config_path: Path, tmp_path: Path, monkeypatch) -> None:
    config_path.write_text('{"agent":{"model":"not-selected"}}')
    selected = tmp_path / "writer.json"
    selected.write_text('{"agent":{"reasoning_effort":"xhigh"}}')
    monkeypatch.setenv("AIWIKI_CONFIG", str(selected))
    assert load_agent_config() == {**DEFAULT_AGENT, "reasoning_effort": "xhigh"}


def test_explicit_missing_config_does_not_fall_back(config_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIWIKI_CONFIG", str(config_path))
    with pytest.raises(ValueError, match="does not exist"):
        load_agent_config()


@pytest.mark.parametrize("contents", [
    b'{"token":"do-not-leak",', b"[]", b"null", b"\xff",
    b'{"agent":null}', b'{"agent":[]}', b'{"agent":"do-not-leak"}',
    b'{"agent":{"api_key":"do-not-leak"}}', b'{"agent":{"model":false}}',
    b'{"agent":{"bin":""}}', b'{"agent":{"model":"  "}}',
    b'{"agent":{"reasoning_effort":"high\\n"}}', b'{"agent":{"bin":"a\\u0000b"}}',
])
def test_invalid_config_fails_without_exposing_contents(config_path: Path, contents: bytes) -> None:
    config_path.write_bytes(contents)
    with pytest.raises(ValueError) as exc:
        load_agent_config()
    assert "do-not-leak" not in str(exc.value)


def test_unreadable_config_is_not_treated_as_absent(config_path: Path) -> None:
    config_path.mkdir()
    with pytest.raises(ValueError, match="cannot read"):
        load_agent_config()


@pytest.mark.parametrize("key", list(DEFAULT_AGENT))
def test_empty_environment_override_fails_closed(config_path: Path, monkeypatch, key: str) -> None:
    monkeypatch.setenv(f"AIWIKI_AGENT_{key.upper()}", "")
    with pytest.raises(ValueError, match=f"AIWIKI_AGENT_{key.upper()}"):
        load_agent_config()


@pytest.mark.parametrize("wrapper", [False, True])
def test_config_reaches_real_subprocess_and_audit_metadata(config_path: Path, tmp_path: Path, wrapper: bool) -> None:
    # A fake Codex executable avoids model calls; the wrapper still executes as a real
    # subprocess with the exact worker argv (including paths containing spaces).
    binary = tmp_path / "fake codex"
    binary.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    binary.chmod(0o700)
    agent_bin = binary
    model = DEFAULT_AGENT["model"]
    effort = DEFAULT_AGENT["reasoning_effort"]
    if wrapper:
        agent_bin = tmp_path / "api wrapper"
        agent_bin.write_text(f'#!/bin/sh\nexec "{binary}" -c \'model_provider="nine_router"\' "$@"\n')
        agent_bin.chmod(0o700)
        model, effort = "gpt-6-astra-combos", "xhigh"
    config_path.write_text(json.dumps({"agent": {
        "bin": str(agent_bin), "model": model, "reasoning_effort": effort,
    }}))
    script = """
import json
from pathlib import Path
from aiwiki.runtime import audit, curate
bundle = Path.cwd()
command = curate._codex_command(bundle, 'test prompt')
result = curate._agent_process(command, cwd=bundle, timeout=10)
print(json.dumps({'argv': json.loads(result.stdout), 'agent': curate._agent_metadata(),
                  'audit_agent': audit.curate._agent_metadata(), 'code': result.returncode}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        text=True, capture_output=True, check=True, timeout=15,
    )
    data = json.loads(result.stdout)
    argv = data["argv"]
    assert data["code"] == 0
    if wrapper:
        assert argv[:2] == ["-c", 'model_provider="nine_router"']
    assert all(index < argv.index("exec") for index, arg in enumerate(argv)
               if arg in ("-c", "--config", "--disable"))
    assert argv[argv.index("exec") + 1:argv.index("exec") + 3] == ["--model", model]
    assert argv[argv.index("--model") + 1] == model
    assert f'model_reasoning_effort="{effort}"' in argv
    assert "sandbox_workspace_write.network_access=false" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert argv[-1] == "test prompt"
    assert data["agent"] == data["audit_agent"] == {
        "runtime": "codex", "bin": str(agent_bin), "model": model, "reasoning_effort": effort,
    }


def test_missing_wrapper_never_falls_back_to_codex(config_path: Path, tmp_path: Path) -> None:
    config_path.write_text(json.dumps({"agent": {"bin": str(tmp_path / "missing-wrapper")}}))
    result = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; from aiwiki.runtime import curate; "
         "curate._agent_process(curate._codex_command(Path.cwd(), 'test'), cwd=Path.cwd(), timeout=1)"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode != 0
    assert "FileNotFoundError" in result.stderr
    assert "missing-wrapper" in result.stderr
