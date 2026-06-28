"""CLI config + server-side bundle commands: connect, active resolution, list/use/create,
legacy config migration. The network layer (_api/_post) is stubbed."""
from __future__ import annotations

import json
from pathlib import Path

from aiwiki.cli import main as cli


def _point_config(monkeypatch, tmp_path: Path) -> Path:
    p = tmp_path / "config.json"
    monkeypatch.setattr(cli, "CONFIG", p)
    return p


def test_config_set_and_conn(monkeypatch, tmp_path: Path) -> None:
    p = _point_config(monkeypatch, tmp_path)
    assert cli.main(["config", "set", "--endpoint", "https://h/", "--token", "tok"]) == 0
    cfg = json.loads(p.read_text())
    assert cfg == {"endpoint": "https://h/", "token": "tok", "bundle": None}
    assert cli._conn() == ("https://h/", "tok")
    assert cli._active() is None              # no active bundle yet → server default
    assert cli._active("override") == "override"


def test_use_sets_active_and_minus_b_overrides(monkeypatch, tmp_path: Path) -> None:
    p = _point_config(monkeypatch, tmp_path)
    cli.main(["config", "set", "--endpoint", "https://h/", "--token", "tok"])
    assert cli.main(["bundle", "use", "solvely-web"]) == 0
    assert json.loads(p.read_text())["bundle"] == "solvely-web"
    assert cli._active() == "solvely-web"
    assert cli._active("other") == "other"    # -b override wins


def test_bundle_list_marks_active_and_default(monkeypatch, tmp_path, capsys) -> None:
    _point_config(monkeypatch, tmp_path)
    cli.main(["config", "set", "--endpoint", "https://h/", "--token", "tok"])
    cli.main(["bundle", "use", "kb-b"])
    monkeypatch.setattr(cli, "_api", lambda route, **kw: {
        "bundles": [{"name": "kb-a", "concepts": 3}, {"name": "kb-b", "concepts": 7}],
        "default": "kb-a",
    })
    assert cli.main(["bundle", "list"]) == 0
    out = capsys.readouterr().out
    assert "* kb-b" in out and "7 concepts" in out
    assert "kb-a" in out and "(default)" in out


def test_bundle_create_posts_and_switches(monkeypatch, tmp_path) -> None:
    p = _point_config(monkeypatch, tmp_path)
    cli.main(["config", "set", "--endpoint", "https://h/", "--token", "tok"])
    seen = {}

    def fake_post(route, payload, **kw):
        seen["route"], seen["payload"] = route, payload
        return {"name": payload["name"], "created": True, "concepts": 0}

    monkeypatch.setattr(cli, "_post", fake_post)
    assert cli.main(["bundle", "create", "fresh-kb"]) == 0
    assert seen == {"route": "/bundles", "payload": {"name": "fresh-kb"}}
    assert json.loads(p.read_text())["bundle"] == "fresh-kb"  # auto-switched to the new bundle


def test_active_bundle_is_sent_as_query_param(monkeypatch, tmp_path) -> None:
    _point_config(monkeypatch, tmp_path)
    cli.main(["config", "set", "--endpoint", "https://h/", "--token", "tok"])
    cli.main(["bundle", "use", "kb-x"])
    calls = []
    monkeypatch.setattr(cli, "_api", lambda route, **kw: calls.append((route, kw)) or
                        {"bundle": "kb-x", "concepts": 0, "by_type": {}, "by_status": {}})
    cli.main(["health"])                       # uses active
    assert calls[-1][1]["bundle"] == "kb-x"
    cli.main(["-b", "kb-y", "health"])          # -b override
    assert calls[-1][1]["bundle"] == "kb-y"


def test_not_configured_errors(monkeypatch, tmp_path) -> None:
    _point_config(monkeypatch, tmp_path)
    import pytest
    with pytest.raises(SystemExit):
        cli._conn()


def test_legacy_flat_config_is_read(monkeypatch, tmp_path: Path) -> None:
    p = _point_config(monkeypatch, tmp_path)
    p.write_text('{"endpoint": "https://old/", "token": "tok"}')
    assert cli._conn() == ("https://old/", "tok")
    assert cli._active() is None


def test_ingest_multiple_files_base64(monkeypatch, tmp_path: Path) -> None:
    import base64
    _point_config(monkeypatch, tmp_path)
    cli.main(["config", "set", "--endpoint", "https://h/", "--token", "tok"])
    cli.main(["bundle", "use", "kb"])
    (tmp_path / "f1.md").write_text("alpha", encoding="utf-8")
    (tmp_path / "f2.pdf").write_bytes(b"%PDF-1.4 binary\x00bytes")  # binary survives via base64
    posts = []

    def fake_post(route, payload, **kw):
        posts.append((payload["filename"], base64.b64decode(payload["content_b64"]), kw.get("bundle")))
        return {"source": f"sources/inbox/{payload['filename']}", "id": f"job{len(posts)}", "curation": "queued"}

    monkeypatch.setattr(cli, "_post", fake_post)
    assert cli.main(["ingest", str(tmp_path / "f1.md"), str(tmp_path / "f2.pdf")]) == 0
    # one POST per file, raw bytes round-trip through base64, filename + active bundle carried
    assert posts == [("f1.md", b"alpha", "kb"), ("f2.pdf", b"%PDF-1.4 binary\x00bytes", "kb")]


def test_ingest_stdin_text(monkeypatch, tmp_path: Path) -> None:
    import io
    _point_config(monkeypatch, tmp_path)
    cli.main(["config", "set", "--endpoint", "https://h/", "--token", "tok"])
    captured = {}
    monkeypatch.setattr(cli, "_post", lambda route, payload, **kw: captured.update(payload) or
                        {"source": "sources/inbox/x.md", "id": "j1", "curation": "queued"})
    monkeypatch.setattr("sys.stdin", io.StringIO("pasted note"))
    assert cli.main(["ingest", "--title", "My Note"]) == 0
    assert captured == {"text": "pasted note", "title": "My Note"}  # stdin → text path


def test_legacy_multi_endpoint_config_migrates(monkeypatch, tmp_path: Path) -> None:
    p = _point_config(monkeypatch, tmp_path)
    # the old {current, bundles:{name:{endpoint,token}}} schema: adopt the active one's conn
    p.write_text(json.dumps({
        "current": "aliyun",
        "bundles": {"aliyun": {"endpoint": "https://a/", "token": "t1"},
                    "local": {"endpoint": "http://127.0.0.1:8787", "token": "t2"}},
    }))
    assert cli._conn() == ("https://a/", "t1")
    assert cli._active() is None
