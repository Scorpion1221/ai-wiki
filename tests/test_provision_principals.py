"""scripts/provision_principals.py (design §8.2): what it writes is exactly what auth.py loads,
and whatever auth.py would refuse at startup never reaches the file."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from aiwiki.runtime import secrets as secret_rules
from aiwiki.service import auth

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "provision_principals.py"
SPEC = importlib.util.spec_from_file_location("provision_principals", SCRIPT)
prov = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prov)

LEGACY = "legacy-shared-token"
MAINTAINER_LIMITS = {"changesets_per_hour": 30, "changesets_per_day": 150, "deprecations_per_day": 10}
BOTH = frozenset({"solvely-wiki", "solvely-wiki-shadow"})
# preset: (id, token prefix, role as /whoami and `doctor --role` see it, bundles, limits)
EXPECTED = {
    "owner": ("human:guobaoqi", "aiw_h_", "admin", None, {}),
    "maintainer": ("process:ai-wiki-maintainer", "aiw_c_", "curator", BOTH, MAINTAINER_LIMITS),
    "shadow-maintainer": ("process:ai-wiki-maintainer-shadow", "aiw_c_", "curator",
                          frozenset({"solvely-wiki-shadow"}), MAINTAINER_LIMITS),
    "auditor": ("process:ai-wiki-auditor", "aiw_a_", "auditor", BOTH, {"reviews_per_day": 200}),
    "watchdog": ("process:ai-wiki-watchdog", "aiw_r_", "reader", None, {}),
    "member": ("member:alice", "aiw_m_", "member", None, {}),
}


def _registry(path: Path, token: str | None = None) -> auth.Registry:
    return auth.Registry.from_env({"AIWIKI_PRINCIPALS": str(path), **({"AIWIKI_TOKEN": token} if token else {})})


def test_every_preset_round_trips_through_auth(tmp_path: Path) -> None:
    path = tmp_path / "principals.json"
    prov.add_legacy(path, LEGACY)
    tokens = {name: prov.add(path, name, pid="member:alice" if name == "member" else None) for name in EXPECTED}
    assert set(prov.PRESETS) == set(EXPECTED) and len(set(tokens.values())) == len(tokens)

    registry = _registry(path, LEGACY)  # the service starts on it while AIWIKI_TOKEN is still set
    stored = path.read_text(encoding="utf-8")
    for name, (pid, prefix, role, bundles, limits) in EXPECTED.items():
        token = tokens[name]
        assert token.startswith(prefix) and token not in stored  # only the digest is at rest
        assert secret_rules.scan(token) == [("ai_wiki_token", 1)]  # G11 and redaction recognise a leaked one
        principal = registry.authorize(f"Bearer {token}")
        assert (principal.id, principal.prefix, principal.role) == (pid, prefix, role), name
        assert principal.scopes == (auth.SCOPES if role == "admin" else auth.ROLES[role])  # doctor: exact
        assert principal.actor == (None if role == "member" else pid)
        assert principal.bundles == bundles and dict(principal.limits) == limits and principal.expires is None


def test_legacy_token_keeps_todays_identity_and_scopes(tmp_path: Path) -> None:
    path = tmp_path / "principals.json"
    owner = prov.add(path, "owner")
    with pytest.raises(auth.PrincipalsError, match="AIWIKI_TOKEN is set, but no principal"):
        prov.check(path, LEGACY)  # the writer and the mirror would refuse to start on it
    prov.add_legacy(path, LEGACY)
    assert prov.check(path, LEGACY) == ("ok: 2 principals: human:guobaoqi, member:legacy-token\n"
                                        "legacy token: held by member:legacy-token")
    # Exactly the principal the legacy token is without a file: same id, every scope, every bundle.
    header = f"Bearer {LEGACY}"
    assert _registry(path, LEGACY).authorize(header) == auth.Registry.from_env({"AIWIKI_TOKEN": LEGACY}).authorize(
        header)
    assert json.loads(path.read_text(encoding="utf-8"))["principals"][-1] == {
        "id": "member:legacy-token", "token_sha256": auth.token_sha256(LEGACY), "scopes": sorted(auth.SCOPES)}

    with pytest.raises(auth.PrincipalsError, match="duplicate principal id"):
        prov.add_legacy(path, LEGACY)
    with pytest.raises(auth.PrincipalsError, match="empty or unset"):
        prov.add_legacy(path, "")
    prov.remove(path, auth.LEGACY_ID)
    with pytest.raises(auth.PrincipalsError, match="token_sha256 is shared with another principal"):
        prov.add_legacy(path, owner)  # one token, two identities: the service would refuse to start


@pytest.mark.parametrize(("preset", "options", "message"), [
    # The id keeps the preset's kind, which decides the actor (and keeps the owner's scopes off a process).
    ("owner", {"pid": "process:rogue"}, "preset owner needs --id human:<name>"),
    ("maintainer", {"pid": "member:helper"}, "preset maintainer needs --id process:<name>"),
    ("maintainer", {"pid": "process:"}, "process:<name>"),
    # auth.py's startup rules.
    ("maintainer", {}, "process:ai-wiki-maintainer: duplicate principal id"),
    ("maintainer", {"limits": {"changeset_per_day": 5}}, "unknown limits changeset_per_day"),
    ("maintainer", {"limits": {"changesets_per_day": -1}}, "non-negative"),
    ("watchdog", {"expires": "soon"}, "expires must be a YYYY-MM-DD date"),
    ("watchdog", {"bundles": [""]}, "bundles must be a list of bundle names"),
    ("member", {}, "preset member needs --id"),
])
def test_refused_edits_leave_the_file_as_it_was(tmp_path: Path, preset: str, options: dict, message: str) -> None:
    path = tmp_path / "principals.json"
    prov.add(path, "maintainer")
    before = path.read_bytes()
    with pytest.raises(auth.PrincipalsError, match=message):
        prov.add(path, preset, **options)
    assert path.read_bytes() == before and [p.name for p in tmp_path.iterdir()] == ["principals.json"]


def test_remove_can_repair_a_file_the_service_would_refuse(tmp_path: Path) -> None:
    path = tmp_path / "principals.json"
    token = prov.add(path, "owner")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["principals"].append({"id": "process:hand-edited", "token_sha256": "0" * 64,
                               "scopes": ["read", "curate", "audit"]})
    path.write_text(json.dumps(data), encoding="utf-8")
    for refused in (lambda: prov.check(path, None), lambda: prov.listing(path), lambda: prov.add(path, "watchdog")):
        with pytest.raises(auth.PrincipalsError, match="must not hold both curate and audit"):
            refused()

    prov.remove(path, "process:hand-edited")
    assert prov.check(path, None).startswith("ok: 1 principals: human:guobaoqi\n")
    assert _registry(path).authorize(f"Bearer {token}", "admin").id == "human:guobaoqi"
    with pytest.raises(auth.PrincipalsError, match="process:hand-edited: no such principal"):
        prov.remove(path, "process:hand-edited")


def test_writes_are_atomic_0640_and_keep_owner_and_group(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "principals.json"
    prov.add(path, "owner")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    path.chmod(0o600)
    inode = path.stat().st_ino
    prov.add(path, "watchdog")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.stat().st_ino != inode  # replaced whole: a reader never sees half a file
    before = path.read_bytes()

    def fail(*_args):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(prov.os, "replace", fail)
        with pytest.raises(OSError, match="disk full"):
            prov.add(path, "member", pid="member:bob")
    assert path.read_bytes() == before and [p.name for p in tmp_path.iterdir()] == ["principals.json"]

    # As root on the writer host: an existing file keeps its owner and group, a new one takes its
    # directory's (root:<writer user>), or the writer could no longer read it.
    chowns: list[tuple[int, int]] = []
    monkeypatch.setattr(prov.os, "chown", lambda _path, uid, gid: chowns.append((uid, gid)))
    real_stat = Path.stat

    def stat_as_root(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self not in (path, tmp_path):
            return result
        fields = list(result[:10])
        fields[4:6] = [0, 4242]  # st_uid, st_gid
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", stat_as_root)
    prov.add(path, "member", pid="member:alice")
    assert chowns == [(0, 4242)]
    chowns.clear()
    prov.add(tmp_path / "new.json", "owner")
    assert chowns == [(os.getuid(), 4242)]


def test_list_shows_no_token_or_digest(tmp_path: Path, capsys) -> None:
    path = tmp_path / "principals.json"
    token = prov.add(path, "maintainer", expires="2026-12-31")
    prov.add_legacy(path, LEGACY)
    assert prov.main(["--file", str(path), "list"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == [
        {"id": "process:ai-wiki-maintainer", "role": "curator", "actor": "process:ai-wiki-maintainer",
         "prefix": "aiw_c_", "scopes": ["curate", "read", "submit"], "bundles": sorted(BOTH),
         "limits": MAINTAINER_LIMITS, "expires": "2026-12-31"},
        {"id": "member:legacy-token", "role": "admin", "actor": None, "prefix": None, "scopes": sorted(auth.SCOPES),
         "bundles": None, "limits": {}, "expires": None},
    ]
    for secret in (token, LEGACY, auth.token_sha256(token), auth.token_sha256(LEGACY)):
        assert secret not in out


def test_add_options_adjust_a_preset(tmp_path: Path, capsys) -> None:
    path = tmp_path / "principals.json"
    argv = ["--file", str(path), "add", "member", "--id", "member:alice", "--bundle", "kb-a", "--bundle", "kb-b",
            "--limit", "changesets_per_day=5", "--expires", "2026-12-31"]
    assert prov.main(argv) == 0
    member = _registry(path).authorize(f"Bearer {capsys.readouterr().out.strip()}", today=date(2026, 12, 31))
    assert member.bundles == {"kb-a", "kb-b"} and member.limits == {"changesets_per_day": 5}
    assert member.expires == date(2026, 12, 31)
    # A --limit overrides only its own quota.
    assert prov.main(["--file", str(path), "add", "maintainer", "--limit", "changesets_per_day=60"]) == 0
    maintainer = _registry(path).authorize(f"Bearer {capsys.readouterr().out.strip()}")
    assert maintainer.limits == {**MAINTAINER_LIMITS, "changesets_per_day": 60}
    with pytest.raises(SystemExit):
        prov.main(["--file", str(path), "add", "watchdog", "--limit", "changesets_per_day"])


def test_cli_as_the_operator_runs_it(tmp_path: Path) -> None:
    path = tmp_path / "principals.json"
    env = {name: value for name, value in os.environ.items() if name not in ("AIWIKI_TOKEN", "AIWIKI_PRINCIPALS")}

    def run(*argv: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), "--file", str(path), *argv], env={**env, **extra},
                              capture_output=True, text=True, check=False)

    added = run("add", "maintainer")
    token = added.stdout.strip()
    assert added.returncode == 0 and added.stdout == f"{token}\n"  # the token alone, once, on stdout
    assert token not in added.stderr and "process:ai-wiki-maintainer" in added.stderr
    assert _registry(path).authorize(f"Bearer {token}", "curate").id == "process:ai-wiki-maintainer"

    missing = run("add-legacy")
    assert missing.returncode == 1 and missing.stdout == "" and "unset" in missing.stderr
    assert run("add-legacy", AIWIKI_TOKEN=LEGACY).returncode == 0
    checked = run("check", AIWIKI_TOKEN=LEGACY)
    assert checked.returncode == 0 and checked.stdout.endswith("legacy token: held by member:legacy-token\n")
    unheld = run("check", AIWIKI_TOKEN="another-shared-token")
    assert unheld.returncode == 1 and unheld.stderr.startswith("refused: AIWIKI_TOKEN is set")
    # --file defaults to $AIWIKI_PRINCIPALS, as the service reads it.
    listed = subprocess.run([sys.executable, str(SCRIPT), "list"], env={**env, "AIWIKI_PRINCIPALS": str(path)},
                            capture_output=True, text=True, check=False)
    assert [row["id"] for row in json.loads(listed.stdout)] == ["process:ai-wiki-maintainer", "member:legacy-token"]

    assert run("remove", "process:ai-wiki-maintainer").returncode == 0
    with pytest.raises(auth.AuthError):
        _registry(path).authorize(f"Bearer {token}")
    again = run("remove", "process:ai-wiki-maintainer")
    assert again.returncode == 1 and "no such principal" in again.stderr
    assert run("check").returncode == 0 and run("list", "--oops").returncode == 2
