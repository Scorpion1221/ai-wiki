"""Principals (design §8.2): startup invariants, hashed-token compare, reload, and — through
the service — the scope matrix, SIGHUP, legacy AIWIKI_TOKEN compatibility and /whoami."""
from __future__ import annotations

import asyncio
import importlib
import json
import signal
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiwiki.service import auth

TOKENS = {
    "curator": "aiw_c_curator-fixture",
    "auditor": "aiw_a_auditor-fixture",
    "watchdog": "aiw_r_watchdog-fixture",
    "member": "aiw_m_member-fixture",
    "owner": "aiw_h_owner-fixture",
}
SPECS = {
    "curator": {"id": "process:ai-wiki-maintainer", "prefix": "aiw_c_", "scopes": ["read", "submit", "curate"],
                "bundles": ["kb-a"], "limits": {"changesets_per_hour": 30, "changesets_per_day": 150}},
    "auditor": {"id": "process:ai-wiki-auditor", "prefix": "aiw_a_", "scopes": ["read", "audit"]},
    "watchdog": {"id": "process:ai-wiki-watchdog", "prefix": "aiw_r_", "scopes": ["read"]},
    "member": {"id": "member:alice", "scopes": ["read", "submit"]},
    "owner": {"id": "human:owner", "prefix": "aiw_h_",
              "scopes": ["read", "submit", "curate", "audit", "human_verify", "admin"]},
}
LEGACY = "legacy-shared-token"


def _entry(name: str, **extra) -> dict:
    return {**SPECS[name], "token_sha256": auth.token_sha256(TOKENS[name]), **extra}


def _write(path: Path, entries: list[dict]) -> Path:
    path.write_text(json.dumps({"principals": entries}), encoding="utf-8")
    return path


# --- principals file -------------------------------------------------------------------

def test_process_principal_cannot_hold_curate_and_audit(tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad.json", [_entry("curator", scopes=["read", "curate", "audit"])])
    with pytest.raises(auth.PrincipalsError, match="process:ai-wiki-maintainer.*curate and audit"):
        auth.Registry.from_env({"AIWIKI_PRINCIPALS": str(bad)})
    # Nor reach verification another way: admin submits either changeset kind, and no agent
    # reaches human-reviewed.
    for scopes, held in ((["read", "submit", "curate", "human_verify"], "human_verify"),
                         (["read", "submit", "curate", "admin"], "admin"),
                         (["read", "audit", "admin", "human_verify"], "admin, human_verify"),
                         (["read", "admin"], "admin")):
        with pytest.raises(auth.PrincipalsError, match=f"process:ai-wiki-maintainer.*must not hold {held}$"):
            auth.parse({"principals": [_entry("curator", scopes=scopes)]})
    # Only process identities are bound by it (the phase-1 shared token keeps every scope).
    ok = _write(tmp_path / "ok.json", [_entry("owner"), _entry("member", scopes=sorted(auth.SCOPES))])
    assert [p.id for p in auth.load(ok)] == ["human:owner", "member:alice"]


def test_startup_needs_principals_or_legacy_token() -> None:
    with pytest.raises(auth.PrincipalsError, match="AIWIKI_PRINCIPALS"):
        auth.Registry.from_env({})
    legacy = auth.Registry.from_env({"AIWIKI_TOKEN": LEGACY}).authorize(f"Bearer {LEGACY}", "admin")
    assert legacy.id == auth.LEGACY_ID and legacy.scopes == auth.SCOPES and legacy.bundles is None


@pytest.mark.parametrize(("document", "message"), [
    ([], "principals file must be"),
    ({"principals": [], "extra": 1}, "principals file must be"),
    ({"principals": [{**SPECS["watchdog"], "id": "watchdog"}]}, "process:<name>"),
    ({"principals": [{**SPECS["watchdog"], "id": "proc:watchdog"}]}, "process:<name>"),
    ({"principals": [{**SPECS["watchdog"], "token_sha256": "abc"}]}, "64 hex"),
    ({"principals": [{**SPECS["watchdog"], "token_sha256": "0" * 64, "bundle": ["kb-a"]}]}, "unknown keys bundle"),
    ({"principals": [{**SPECS["watchdog"], "token_sha256": "0" * 64, "scopes": ["read", "wrte"]}]}, "unknown scopes"),
    ({"principals": [{**SPECS["watchdog"], "token_sha256": "0" * 64, "scopes": []}]}, "non-empty"),
    ({"principals": [{**SPECS["watchdog"], "token_sha256": "0" * 64, "bundles": "kb-a"}]}, "bundles"),
    ({"principals": [{**SPECS["watchdog"], "token_sha256": "0" * 64,
                      "limits": {"changesets_per_day": -1}}]}, "non-negative"),
    ({"principals": [{**SPECS["watchdog"], "token_sha256": "0" * 64,
                      "limits": {"changeset_per_day": 150}}]}, "unknown limits changeset_per_day"),
    ({"principals": [{**SPECS["watchdog"], "token_sha256": "0" * 64, "expires": "soon"}]}, "expires"),
    ({"principals": [{**SPECS["watchdog"], "token_sha256": "0" * 64},
                     {**SPECS["watchdog"], "token_sha256": "1" * 64}]}, "duplicate principal id"),
    ({"principals": [{**SPECS["watchdog"], "token_sha256": "0" * 64},
                     {**SPECS["member"], "token_sha256": "0" * 64}]}, "shared with another"),
])
def test_invalid_principals_files_are_refused(document, message) -> None:
    with pytest.raises(auth.PrincipalsError, match=message):
        auth.parse(document)


def test_principal_is_hashable_and_its_limits_read_only() -> None:
    limits = {"changesets_per_day": 150}
    principal = auth.parse({"principals": [_entry("curator", limits=limits)]})[0]
    assert {principal: 1}[principal] == 1  # usable as a key or in a cache
    with pytest.raises(TypeError):
        principal.limits["changesets_per_day"] = 10**6  # type: ignore[index]
    limits["changesets_per_day"] = 10**6  # nor through the parsed document
    assert principal.limits == {"changesets_per_day": 150}
    assert principal == auth.parse({"principals": [_entry("curator", limits={"changesets_per_day": 150})]})[0]


def test_unreadable_principals_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(auth.PrincipalsError, match="cannot read"):
        auth.load(tmp_path / "missing.json")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(auth.PrincipalsError, match="cannot read"):
        auth.load(tmp_path / "broken.json")


def test_hash_compare_is_constant_time_over_every_principal(monkeypatch, tmp_path: Path) -> None:
    entries = [_entry(name) for name in SPECS]
    entries[0]["token_sha256"] = entries[0]["token_sha256"].upper()  # digests are case-insensitive hex
    path = _write(tmp_path / "principals.json", entries)
    assert all(token not in path.read_text() for token in TOKENS.values())  # only digests at rest
    registry = auth.Registry.from_env({"AIWIKI_PRINCIPALS": str(path)})

    calls = []
    real = auth.hmac.compare_digest
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: calls.append(b) or real(a, b))
    assert registry.authorize(f"Bearer {TOKENS['curator']}").id == "process:ai-wiki-maintainer"
    assert len(calls) == len(SPECS)  # no early exit on the first match
    calls.clear()
    with pytest.raises(auth.AuthError) as denied:
        registry.authorize("Bearer wrong")
    assert denied.value.status == 401 and len(calls) == len(SPECS)
    # The stored digest is not itself a credential (no pass-the-hash).
    with pytest.raises(auth.AuthError):
        registry.authorize(f"Bearer {auth.token_sha256(TOKENS['owner'])}")


def test_scopes_and_expiry(tmp_path: Path) -> None:
    path = _write(tmp_path / "principals.json", [_entry("member", expires="2026-12-31")])
    registry = auth.Registry.from_env({"AIWIKI_PRINCIPALS": str(path)})
    header = f"Bearer {TOKENS['member']}"
    assert registry.authorize(header, "curate", "submit", today=date(2026, 12, 31)).id == "member:alice"
    with pytest.raises(auth.AuthError, match="lacks scope curate or audit") as denied:
        registry.authorize(header, "curate", "audit", today=date(2026, 12, 31))
    assert denied.value.status == 403
    with pytest.raises(auth.AuthError, match="expired") as expired:
        registry.authorize(header, "read", today=date(2027, 1, 1))
    assert expired.value.status == 401


def test_reload_swaps_principals_and_keeps_them_on_a_bad_file(tmp_path: Path, caplog) -> None:
    path = _write(tmp_path / "principals.json", [_entry(name) for name in SPECS])
    registry = auth.Registry.from_env({"AIWIKI_PRINCIPALS": str(path)})
    member = f"Bearer {TOKENS['member']}"
    assert registry.authorize(member).id == "member:alice"

    # Incident response: remove a principal, narrow another, reload.
    _write(path, [_entry("owner"), _entry("curator", scopes=["read"])])
    assert registry.reload() is True
    with pytest.raises(auth.AuthError):
        registry.authorize(member)
    with pytest.raises(auth.AuthError) as narrowed:
        registry.authorize(f"Bearer {TOKENS['curator']}", "curate")
    assert narrowed.value.status == 403

    # An invalid file never replaces the principals in force.
    _write(path, [_entry("owner"), _entry("auditor", scopes=["read", "audit", "curate"])])
    assert registry.reload() is False
    assert "reload refused" in caplog.text and "curate and audit" in caplog.text
    assert [p.id for p in registry.principals] == ["human:owner", "process:ai-wiki-maintainer"]
    assert "curate and audit" in registry.receipt()["reload_error"]  # visible beyond the log
    _write(path, [_entry("owner")])
    assert registry.reload() is True
    assert registry.receipt()["principals"] == ["human:owner"] and registry.receipt()["reload_error"] is None

    legacy = auth.Registry.from_env({"AIWIKI_TOKEN": LEGACY})
    assert legacy.reload() is False  # nothing to re-read


def test_check_a_principals_file_before_sighup(tmp_path: Path, capsys) -> None:
    good = _write(tmp_path / "good.json", [_entry("owner"), _entry("member")])
    assert auth.main([str(good)]) == 0
    assert capsys.readouterr().out == "ok: 2 principals: human:owner, member:alice\n"
    bad = _write(tmp_path / "bad.json", [_entry("curator", scopes=["read", "curate", "audit"])])
    assert auth.main([str(bad)]) == 1 and "curate and audit" in capsys.readouterr().err
    assert auth.main([]) == 2
    run = subprocess.run([sys.executable, "-m", "aiwiki.service.auth", str(bad)],
                         capture_output=True, text=True, check=False)
    assert run.returncode == 1 and run.stderr.startswith("refused: process:ai-wiki-maintainer")


def test_sighup_handler_needs_a_principals_file() -> None:
    before = signal.getsignal(signal.SIGHUP)
    with auth.reload_on_sighup(auth.Registry.from_env({"AIWIKI_TOKEN": LEGACY})):
        assert signal.getsignal(signal.SIGHUP) == before  # legacy mode keeps the default action


# --- through the service ---------------------------------------------------------------

MODE_ENV = ("AIWIKI_INTAKE", "AIWIKI_AUDIT", "AIWIKI_CHANGESETS_COMMIT", "AIWIKI_RESTRUCTURE",
            "AIWIKI_CODEX_AUDIT_MANUAL")
# (method, path, query, body, scopes of which any one passes; None = any valid token).
# Bodies/ids are chosen so an authorized call stops at a harmless 4xx/200 without side effects.
ROUTES = [
    ("GET", "/whoami", {}, None, None),
    ("GET", "/bundles", {}, None, {"read"}),
    ("GET", "/health", {"bundle": "kb-a"}, None, {"read"}),
    ("GET", "/ls", {"bundle": "kb-a"}, None, {"read"}),
    ("GET", "/cat", {"bundle": "kb-a", "path": "topics/kb-a.md"}, None, {"read"}),
    ("GET", "/grep", {"bundle": "kb-a", "q": "Alpha"}, None, {"read"}),
    ("GET", "/search", {"bundle": "kb-a", "q": "Alpha"}, None, {"read"}),
    ("GET", "/links", {"bundle": "kb-a", "path": "topics/kb-a.md"}, None, {"read"}),
    ("GET", "/log", {"bundle": "kb-a"}, None, {"read"}),
    ("GET", "/jobs/pending-audit", {"bundle": "kb-a"}, None, {"read"}),
    ("GET", "/jobs/no-such-job", {"bundle": "kb-a"}, None, {"read"}),
    ("POST", "/ingest", {"bundle": "kb-a"}, {}, {"submit"}),
    ("POST", "/jobs/no-such-job/audit", {"bundle": "kb-a"}, {}, {"audit", "curate"}),
    ("POST", "/bundles", {}, {"name": "!invalid"}, {"admin"}),
    ("DELETE", "/bundles/kb-missing", {}, None, {"admin"}),
    # Changesets: the kind picks the scope; an authorized call stops at G1 (400) or the writer 403.
    ("GET", "/workspace", {"bundle": "kb-a"}, None, {"read"}),
    ("POST", "/changesets", {"bundle": "kb-a"}, {}, {"curate", "audit", "admin"}),
    ("POST", "/changesets", {"bundle": "kb-a", "dry_run": "true"}, {"kind": "curate"}, {"curate", "admin"}),
    ("POST", "/changesets", {"bundle": "kb-a", "dry_run": "true"}, {"kind": "audit"}, {"audit", "admin"}),
    # Maintenance state; with AIWIKI_CURATE=off an authorized call stops at the writer 403.
    ("POST", "/maint/lease/maintainer", {"bundle": "kb-a"}, None, {"curate"}),
    ("DELETE", "/maint/lease/maintainer", {"bundle": "kb-a"}, None, {"curate"}),
    ("POST", "/maint/lease/auditor", {"bundle": "kb-a"}, None, {"audit"}),
    ("DELETE", "/maint/lease/auditor", {"bundle": "kb-a"}, None, {"audit"}),
    ("GET", "/maint/cursors/repos", {"bundle": "kb-a"}, None, {"read"}),
    ("PUT", "/maint/cursors/repos", {"bundle": "kb-a"}, {}, {"curate"}),
    ("POST", "/maint/items", {"bundle": "kb-a"}, {}, {"curate"}),
    ("GET", "/maint/items", {"bundle": "kb-a"}, None, {"read"}),
    ("POST", "/maint/items/next", {"bundle": "kb-a"}, None, {"curate"}),
    ("GET", "/maint/items/it_000000000000", {"bundle": "kb-a"}, None, {"read"}),
    ("POST", "/maint/items/it_000000000000/files", {"bundle": "kb-a"}, {}, {"curate"}),
    ("GET", "/maint/items/it_000000000000/files/a.md", {"bundle": "kb-a"}, None, {"read"}),
    ("POST", "/maint/items/it_000000000000/resolve", {"bundle": "kb-a"}, {}, {"curate"}),
    ("GET", "/maint/status", {"bundle": "kb-a"}, None, {"read"}),
    ("POST", "/admin/items/it_000000000000/retry", {"bundle": "kb-a"}, {}, {"admin"}),
    ("POST", "/admin/items/it_000000000000/resolve", {"bundle": "kb-a"}, {}, {"admin"}),
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for name in ("kb-a", "kb-b"):
        bundle = tmp_path / "bundles" / name
        (bundle / "topics").mkdir(parents=True)
        (bundle / "index.md").write_text('---\nokf_version: "0.2"\n---\n\n# KB\n', encoding="utf-8")
        (bundle / "topics" / f"{name}.md").write_text(
            "---\ntype: Reference\ntitle: Alpha\ndescription: Fixture concept.\ntags: [x]\nstatus: draft\n"
            "generated: {by: process:test, at: 2026-08-13T00:00:00Z}\n"
            "sources:\n  - {id: fixture, resource: https://example.com/x}\n---\n# Summary\n\nAlpha.\n",
            encoding="utf-8",
        )
    return tmp_path / "bundles"


@pytest.fixture
def principals(tmp_path: Path) -> Path:
    return _write(tmp_path / "principals.json", [_entry(name) for name in SPECS])


def _app(monkeypatch, root: Path, *, principals: Path | None = None, token: str | None = None, **env):
    monkeypatch.setenv("AIWIKI_BUNDLES", str(root))
    monkeypatch.delenv("AIWIKI_BUNDLE", raising=False)
    monkeypatch.delenv("AIWIKI_DEFAULT_BUNDLE", raising=False)
    monkeypatch.setenv("AIWIKI_CURATE", "off")
    monkeypatch.setenv("AIWIKI_DISABLE", "")
    for name in MODE_ENV:
        monkeypatch.delenv(name, raising=False)
    if principals is not None:
        monkeypatch.setenv("AIWIKI_PRINCIPALS", str(principals))
    if token is not None:
        monkeypatch.setenv("AIWIKI_TOKEN", token)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    from aiwiki.service import app as appmod
    return importlib.reload(appmod)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _call(client: TestClient, method: str, path: str, query: dict, body, token: str):
    return client.request(method, path, params=query, json=body, headers=_bearer(token))


def _denied(response) -> bool:
    return response.status_code == 403 and "lacks scope" in response.json()["detail"]


@pytest.mark.parametrize("role", sorted(SPECS))
def test_scope_matrix(monkeypatch, root: Path, principals: Path, role: str) -> None:
    client = TestClient(_app(monkeypatch, root, principals=principals).app)
    held = set(SPECS[role]["scopes"])
    for method, path, query, body, scopes in ROUTES:
        response = _call(client, method, path, query, body, TOKENS[role])
        assert response.status_code != 401, (role, path)
        allowed = scopes is None or bool(held & scopes)
        assert _denied(response) is not allowed, (role, method, path, response.status_code, response.text)


def test_bundle_restriction(monkeypatch, root: Path, principals: Path) -> None:
    client = TestClient(_app(monkeypatch, root, principals=principals).app)
    curator = _bearer(TOKENS["curator"])
    assert client.get("/health", params={"bundle": "kb-a"}, headers=curator).status_code == 200
    for name in ("kb-b", "kb-missing"):  # existing or not: the same 403, no existence oracle
        response = client.get("/health", params={"bundle": name}, headers=curator)
        assert response.status_code == 403 and "may not access bundle" in response.json()["detail"]
    assert [b["name"] for b in client.get("/bundles", headers=curator).json()["bundles"]] == ["kb-a"]
    owner = client.get("/bundles", headers=_bearer(TOKENS["owner"])).json()
    assert [b["name"] for b in owner["bundles"]] == ["kb-a", "kb-b"]


def test_a_disallowed_default_bundle_is_never_named(monkeypatch, root: Path, principals: Path) -> None:
    client = TestClient(_app(monkeypatch, root, principals=principals, AIWIKI_DEFAULT_BUNDLE="kb-b").app)
    curator = _bearer(TOKENS["curator"])  # limited to kb-a
    assert client.get("/bundles", headers=curator).json()["default"] is None
    response = client.get("/health", headers=curator)
    assert response.status_code == 400 and "kb-b" not in response.text
    assert client.get("/health", headers=_bearer(TOKENS["owner"])).json()["bundle"] == "kb-b"
    client = TestClient(_app(monkeypatch, root, principals=principals, AIWIKI_DEFAULT_BUNDLE="kb-a").app)
    assert client.get("/health", headers=curator).json()["bundle"] == "kb-a"


def test_unknown_and_missing_tokens_are_401(monkeypatch, root: Path, principals: Path) -> None:
    client = TestClient(_app(monkeypatch, root, principals=principals).app)
    assert client.get("/whoami").status_code == 401
    assert client.get("/whoami", headers={"Authorization": TOKENS["owner"]}).status_code == 401
    assert client.get("/whoami", headers=_bearer("nope")).status_code == 401
    assert client.get("/whoami", headers=_bearer("")).status_code == 401
    # A principals file supersedes the legacy shared token.
    assert client.get("/whoami", headers=_bearer(LEGACY)).status_code == 401


def test_a_principals_file_must_carry_the_legacy_token_still_in_the_environment(monkeypatch, root: Path,
                                                                                tmp_path: Path) -> None:
    # Phase 1 moves eb17 into the file. Leaving it out would 401 every member and the P0 maintain
    # at their next call; startup says so instead, on the writer and the mirror alike.
    without = _write(tmp_path / "without.json", [_entry("owner")])
    with pytest.raises(auth.PrincipalsError, match="AIWIKI_TOKEN is set, but no principal in .* holds its sha256"):
        _app(monkeypatch, root, principals=without, token=LEGACY)

    legacy = {"id": "member:legacy-eb17", "scopes": sorted(auth.SCOPES), "token_sha256": auth.token_sha256(LEGACY)}
    client = TestClient(_app(monkeypatch, root, principals=_write(tmp_path / "with.json", [_entry("owner"), legacy]),
                             token=LEGACY).app)
    assert client.get("/whoami", headers=_bearer(LEGACY)).json()["principal"] == "member:legacy-eb17"


def test_service_refuses_to_start_on_invalid_principals(monkeypatch, root: Path, tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad.json", [_entry("auditor", scopes=["read", "audit", "curate"])])
    with pytest.raises(auth.PrincipalsError, match="process:ai-wiki-auditor.*curate and audit"):
        _app(monkeypatch, root, principals=bad)


def test_legacy_token_keeps_every_scope(monkeypatch, root: Path) -> None:
    client = TestClient(_app(monkeypatch, root, token=LEGACY).app)
    who = client.get("/whoami", headers=_bearer(LEGACY)).json()
    assert who["principal"] == auth.LEGACY_ID and who["role"] == "admin"
    assert who["scopes"] == sorted(auth.SCOPES) and who["bundles"] is None
    for method, path, query, body, _scopes in ROUTES:
        response = _call(client, method, path, query, body, LEGACY)
        assert response.status_code != 401 and not _denied(response), (path, response.text)


def test_whoami_reports_identity_contract_and_modes(monkeypatch, root: Path, principals: Path) -> None:
    client = TestClient(_app(monkeypatch, root, principals=principals).app)
    body = client.get("/whoami", headers=_bearer(TOKENS["curator"])).json()
    assert set(body) == {"writer", "principal", "actor", "role", "scopes", "bundles", "limits", "remaining_today",
                         "api", "client", "service", "modes"}
    assert body["writer"] is False  # this app runs as a read mirror (AIWIKI_CURATE=off)
    assert body["principal"] == body["actor"] == "process:ai-wiki-maintainer"
    assert body["role"] == "curator"
    assert body["scopes"] == ["curate", "read", "submit"]
    assert body["bundles"] == ["kb-a"]
    assert body["limits"] == {"changesets_per_hour": 30, "changesets_per_day": 150}
    assert body["api"] == {"changesets": 1} and body["client"] == {"min": "0.3.0"}
    assert set(body["service"]) == {"version", "build"}
    # Dark launch: unset switches report today's behaviour.
    assert body["modes"] == {"intake": "curate", "audit": "codex", "changesets_commit": [], "restructure": "off",
                             "codex_audit_manual": []}

    roles = {name: client.get("/whoami", headers=_bearer(TOKENS[name])).json() for name in SPECS}
    assert {name: who["role"] for name, who in roles.items()} == {
        "curator": "curator", "auditor": "auditor", "watchdog": "reader", "member": "member", "owner": "admin",
    }
    assert roles["member"]["actor"] is None  # a member principal is never stamped into frontmatter
    assert roles["owner"]["actor"] == "human:owner" and roles["owner"]["bundles"] is None


def test_admin_whoami_shows_the_principals_in_force(monkeypatch, root: Path, principals: Path) -> None:
    appmod = _app(monkeypatch, root, principals=principals)
    client = TestClient(appmod.app)
    owner = _bearer(TOKENS["owner"])
    receipt = client.get("/whoami", headers=owner).json()["auth"]
    assert receipt["principals"] == [SPECS[name]["id"] for name in SPECS] and receipt["reload_error"] is None
    assert "auth" not in client.get("/whoami", headers=_bearer(TOKENS["auditor"])).json()  # admin only

    # Incident response: alice is revoked, but the edit leaves the file invalid.
    principals.write_text('{"principals": [}', encoding="utf-8")
    assert appmod.AUTH.reload() is False
    receipt = client.get("/whoami", headers=owner).json()["auth"]
    assert "member:alice" in receipt["principals"] and "cannot read" in receipt["reload_error"]
    _write(principals, [_entry("owner")])
    assert appmod.AUTH.reload() is True
    assert client.get("/whoami", headers=owner).json()["auth"]["principals"] == ["human:owner"]


def test_whoami_modes_follow_env_and_bad_values_refuse_start(monkeypatch, root: Path, principals: Path) -> None:
    appmod = _app(monkeypatch, root, principals=principals,
                  AIWIKI_CHANGESETS_COMMIT="kb-b, kb-a,", AIWIKI_CODEX_AUDIT_MANUAL=" kb-b")
    modes = TestClient(appmod.app).get("/whoami", headers=_bearer(TOKENS["owner"])).json()["modes"]
    assert modes["changesets_commit"] == ["kb-a", "kb-b"] and modes["codex_audit_manual"] == ["kb-b"]
    assert appmod.worker.AUDIT_BUNDLES == {"kb-a"}
    # With the route disabled nothing commits, and /whoami says so.
    appmod = _app(monkeypatch, root, principals=principals, AIWIKI_CHANGESETS_COMMIT="kb-a",
                  AIWIKI_DISABLE="changesets")
    modes = TestClient(appmod.app).get("/whoami", headers=_bearer(TOKENS["owner"])).json()["modes"]
    assert modes["changesets_commit"] == [] and appmod.worker.COMMIT_BUNDLES == frozenset()
    with pytest.raises(RuntimeError, match="AIWIKI_INTAKE"):
        _app(monkeypatch, root, principals=principals, AIWIKI_INTAKE="codex")
    # Not honoured yet (the inbox, the audit gate, the restructure intent), so a premature flip
    # refuses to start rather than stop /ingest curation or every audit while /whoami says fine.
    for name, value in (("AIWIKI_INTAKE", "inbox"), ("AIWIKI_AUDIT", "external"), ("AIWIKI_RESTRUCTURE", "on")):
        with pytest.raises(RuntimeError, match=f"{name} must be one of .*; got '{value}'"):
            _app(monkeypatch, root, principals=principals, **{name: value})


def test_external_audit_mode_closes_the_codex_audit_route(monkeypatch, root: Path, principals: Path) -> None:
    appmod = _app(monkeypatch, root, principals=principals)
    monkeypatch.setitem(appmod.MODES, "audit", "external")  # as phase 4a will allow
    client = TestClient(appmod.app)
    for role in ("curator", "auditor", "owner"):
        response = client.post("/jobs/no-such-job/audit", params={"bundle": "kb-a"}, headers=_bearer(TOKENS[role]))
        assert response.status_code == 409 and response.json()["detail"] == "audit is external", role
    watchdog = client.post("/jobs/no-such-job/audit", params={"bundle": "kb-a"}, headers=_bearer(TOKENS["watchdog"]))
    assert _denied(watchdog)


def test_maint_routes_act_as_the_calling_principal(monkeypatch, root: Path, principals: Path) -> None:
    client = TestClient(_app(monkeypatch, root, principals=principals, AIWIKI_CURATE="auto").app)
    kb_a = {"bundle": "kb-a"}
    curator = {**_bearer(TOKENS["curator"]), "X-AIWiki-Run": "WAIO-1"}
    lease = client.post("/maint/lease/maintainer", params=kb_a, headers=curator).json()["lease"]
    assert lease["holder"] == "process:ai-wiki-maintainer" and lease["run"] == "WAIO-1"
    assert client.get("/maint/status", params=kb_a, headers=curator).json()["leases"]["maintainer"]["active"]
    # The same run id under another principal is another holder: no takeover, no release.
    owner = {**_bearer(TOKENS["owner"]), "X-AIWiki-Run": "WAIO-1"}
    held = client.post("/maint/lease/maintainer", params=kb_a, headers=owner)
    assert held.status_code == 409 and held.json()["detail"]["holder"] == "process:ai-wiki-maintainer"
    assert client.delete("/maint/lease/maintainer", params=kb_a, headers=owner).status_code == 409
    # Bundle restrictions hold for maintenance state too.
    denied = client.get("/maint/status", params={"bundle": "kb-b"}, headers=curator)
    assert denied.status_code == 403 and "may not access bundle" in denied.json()["detail"]
    assert client.delete("/maint/lease/maintainer", params=kb_a, headers=curator).json()["released"] is True


@pytest.mark.parametrize("value", [None, "", " codex "])
def test_maint_status_reports_the_audit_mode_whoami_reports(monkeypatch, root: Path, principals: Path,
                                                            value: str | None) -> None:
    env = {} if value is None else {"AIWIKI_AUDIT": value}
    client = TestClient(_app(monkeypatch, root, principals=principals, AIWIKI_CURATE="auto", **env).app)
    owner = _bearer(TOKENS["owner"])
    mode = client.get("/whoami", headers=owner).json()["modes"]["audit"]
    status = client.get("/maint/status", params={"bundle": "kb-a"}, headers=owner).json()
    assert status["audit"]["mode"] == mode
    assert status["auditor_independence"] == ("strong" if mode == "codex" else None)


def test_sighup_reloads_the_running_service(monkeypatch, root: Path, principals: Path) -> None:
    appmod = _app(monkeypatch, root, principals=principals)
    newcomer = "aiw_m_newcomer-fixture"
    before = signal.getsignal(signal.SIGHUP)

    async def serve() -> None:
        async with appmod.lifespan(appmod.app):
            _write(principals, [_entry("owner"), {"id": "member:bob", "scopes": ["read"],
                                                  "token_sha256": auth.token_sha256(newcomer)}])
            # Guard: never raise SIGHUP unless the service's handler would catch it.
            assert signal.getsignal(signal.SIGHUP) not in (signal.SIG_DFL, signal.SIG_IGN, None)
            signal.raise_signal(signal.SIGHUP)

    asyncio.run(serve())
    assert signal.getsignal(signal.SIGHUP) == before  # handler removed at shutdown
    client = TestClient(appmod.app)
    assert client.get("/whoami", headers=_bearer(newcomer)).json()["principal"] == "member:bob"
    assert client.get("/whoami", headers=_bearer(TOKENS["member"])).status_code == 401
