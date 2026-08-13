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
    assert '"kb-b",7,"active"' in out
    assert '"kb-a",3,"default"' in out


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


def test_ingest_duplicate_reports_noop(monkeypatch, tmp_path: Path, capsys) -> None:
    _point_config(monkeypatch, tmp_path)
    cli.main(["config", "set", "--endpoint", "https://h/", "--token", "tok"])
    (tmp_path / "same.md").write_text("same", encoding="utf-8")
    monkeypatch.setattr(cli, "_post", lambda *args, **kwargs: {
        "source": "sources/n.md", "id": "oldjob", "status": "done", "deduplicated": True,
    })

    assert cli.main(["ingest", str(tmp_path / "same.md")]) == 0
    out = capsys.readouterr().out
    assert "submissions[1]{input,source,job,state}:" in out
    assert "no-op:done" in out


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


def test_version_fast_paths_are_bare(capsys) -> None:
    from aiwiki.cli import entry
    from aiwiki.version import VERSION

    project = Path(__file__).parents[1] / "pyproject.toml"
    import tomllib

    assert tomllib.loads(project.read_text())["project"]["version"] == VERSION

    assert entry.main(["--version"]) == 0
    assert capsys.readouterr().out == f"{VERSION}\n"
    assert cli.main(["-V"]) == 0
    assert capsys.readouterr().out == f"{VERSION}\n"


def test_home_view_is_live_content(monkeypatch, tmp_path: Path, capsys) -> None:
    _point_config(monkeypatch, tmp_path)
    cli.main(["config", "set", "--endpoint", "https://h/", "--token", "tok"])
    capsys.readouterr()

    def fake_api(route, **kw):
        if route == "/health":
            return {
                "bundle": "kb", "concepts": 7, "okf_version": "0.2",
                "by_status": {"stable": 7}, "by_trust": {"unverified": 7},
                "by_freshness": {"unspecified": 7},
            }
        assert route == "/ls"
        return {"items": [{"path": "metrics/", "kind": "dir", "concepts": 3}]}

    monkeypatch.setattr(cli, "_api", fake_api)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "bin:" in out and "description:" in out
    assert 'name: "kb"' in out and "concepts: 7" in out
    assert 'okf_version: "0.2"' in out
    assert "trust_counts:" in out and "freshness_counts:" in out
    assert 'directories[1]{path,concepts}:\n  "metrics/",3' in out
    assert "usage:" not in out


def test_usage_errors_are_structured_on_stdout(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit) as exc:
        cli.main(["search", "--stat", "closed"])
    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert captured.err == ""
    assert "error:" in captured.out
    assert 'kind: "usage"' in captured.out
    assert "unrecognized arguments: --stat" in captured.out
    assert '"ai-wiki search --help"' in captured.out


def test_bundle_rm_requires_flag_without_prompt_or_api(monkeypatch, capsys) -> None:
    import pytest

    monkeypatch.setattr(cli, "_post", lambda *a, **kw: pytest.fail("must validate before API call"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["bundle", "rm", "old-kb"])
    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert captured.err == ""
    assert '"ai-wiki bundle rm old-kb --yes"' in captured.out


def test_cat_truncates_with_full_escape_hatch(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_api", lambda *a, **kw: {"content": "abcdefghij"})
    assert cli.main(["cat", "x.md", "--max-chars", "4"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("abcd\n")
    assert "truncated at 4 of 10 chars" in out
    assert "ai-wiki cat x.md --full" in out

    assert cli.main(["cat", "x.md", "--full"]) == 0
    assert capsys.readouterr().out == "abcdefghij"


def test_cat_json_and_read_tables_surface_okf_metadata(monkeypatch, capsys) -> None:
    metadata = {
        "status": "stable", "trust": "machine-confirmed", "freshness": "fresh",
        "verification_current": True,
        "generated_at": "2026-08-13T10:00:00Z", "verified_at": "2026-08-13T11:00:00Z",
        "current_verified_at": "2026-08-13T11:00:00Z",
    }

    def fake_api(route, **_kwargs):
        if route == "/cat":
            return {"path": "x.md", "content": "body", "metadata": metadata}
        if route == "/ls":
            return {"items": [{"path": "x.md", "kind": "concept", "title": "X", **metadata}]}
        if route == "/search":
            return {"results": [{"path": "x.md", "title": "X", "score": 8, **metadata}], "total": 1}
        raise AssertionError(route)

    monkeypatch.setattr(cli, "_api", fake_api)
    assert cli.main(["cat", "x.md", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["metadata"] == metadata

    assert cli.main(["ls"]) == 0
    ls_out = capsys.readouterr().out
    assert "verification_current,generated_at,verified_at,current_verified_at" in ls_out
    assert '"machine-confirmed","fresh",true' in ls_out

    assert cli.main(["search", "x"]) == 0
    search_out = capsys.readouterr().out
    assert "verification_current,generated_at,verified_at,current_verified_at" in search_out
    assert '"2026-08-13T10:00:00Z","2026-08-13T11:00:00Z"' in search_out


def test_grep_limit_reports_total_and_escape_hatch(monkeypatch, capsys) -> None:
    hits = [{"path": "x.md", "line": i, "text": f"hit {i}"} for i in range(3)]
    monkeypatch.setattr(cli, "_api", lambda *a, **kw: {"hits": hits})
    assert cli.main(["grep", "hit", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "shown: 2" in out and "total: 3" in out and "truncated: true" in out
    assert "hits[2]{path,line,text}:" in out
    assert '"ai-wiki grep \\"hit\\" --limit 0"' in out


def test_ingest_title_rejects_multiple_inputs_before_api(monkeypatch, capsys) -> None:
    import pytest

    monkeypatch.setattr(cli, "_post", lambda *a, **kw: pytest.fail("must validate before API call"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["ingest", "a.md", "b.md", "--title", "ignored before"])
    assert exc.value.code == 2
    assert "--title requires exactly one input" in capsys.readouterr().out


def test_audit_posts_under_jobs_and_reuses_jobs_polling(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(cli, "_post", lambda route, payload, **kw: calls.append((route, payload, kw)) or {
        "id": "audit1", "kind": "audit", "parent_job": "ingest1", "status": "queued",
    })
    assert cli.main(["-b", "kb", "audit", "ingest1"]) == 0
    assert calls == [("/jobs/ingest1/audit", {}, {"bundle": "kb"})]
    out = capsys.readouterr().out
    assert "audit1" in out and "ai-wiki jobs audit1" in out
