"""scripts/deploy_bwg.sh, the production deploy (writer + read mirror on the AI Wiki host): it parses, and bad usage
is refused before the script reads a repository or reaches the host. It never runs against a host here."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_bwg.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not installed")


def _run(*args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    # ssh and scp on PATH fail loudly, so a refusal that reached the host would show.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("ssh", "scp"):
        fake = bin_dir / tool
        fake.write_text(f"#!/bin/sh\necho 'called {tool}' >&2\nexit 99\n")
        fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30)


def test_parses_and_is_executable():
    assert subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True).returncode == 0
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("args", [(), ("--dry-run",), ("only-one-arg",), ("a", "b", "c")])
def test_bad_usage_is_refused_before_the_host(tmp_path, args):
    done = _run(*args, tmp_path=tmp_path)
    assert done.returncode == 2
    assert "usage:" in done.stderr
    assert "called ssh" not in done.stderr and "called scp" not in done.stderr
