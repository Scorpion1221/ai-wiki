"""Stage 3 tests: GET /workspace delta pull + disk cache + client stat cache + validate->propose dedup."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import threading
import time
from pathlib import Path

import pytest
from gate_fixture import EVIDENCE, EVIDENCE_ID, METRIC, Gate, cite, cited, clone, git

from aiwiki.cli import main as cli
from aiwiki.cli import workspace

VIEWS = "metrics/view-references-exposure-proxy-2026-09.md"


def _wiki(capsys, *args) -> tuple[int, str]:
    capsys.readouterr()
    try:
        code = cli.main([str(arg) for arg in args])
    except SystemExit as exit_:
        code = exit_.code
    return code, capsys.readouterr().out


def _wiki_json(capsys, *args) -> tuple[int, dict]:
    code, out = _wiki(capsys, *args, "--json")
    return code, json.loads(out[out.rfind("\n{\n") + 1:] if not out.startswith("{") else out)


def _tree_snapshot(root: Path, *, skip_meta: bool = False) -> dict[str, bytes]:
    snap: dict[str, bytes] = {}
    for directory, dirnames, filenames in os.walk(root):
        base = Path(directory)
        if skip_meta and base == root:
            dirnames[:] = [d for d in dirnames if d not in (".ai-wiki", ".git")]
        for name in filenames:
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            snap[path.relative_to(root).as_posix()] = path.read_bytes()
    return dict(sorted(snap.items()))


def _push_commit(gate: Gate, mutate) -> str:
    """Apply ``mutate(other_clone)`` on a fresh clone, commit, push, and fetch on the writer."""
    other = clone(gate.remote, gate.tmp / f"other-{time.time_ns()}")
    mutate(other)
    git(other, "add", "-f", "-A")
    git(other, "commit", "-qm", "stage3 test commit")
    git(other, "push", "-q", "origin", "main")
    git(gate.writer, "fetch", "-q")
    return git(gate.writer, "rev-parse", "refs/remotes/origin/main")


@pytest.fixture
def gate(tmp_path, monkeypatch):
    g = Gate(tmp_path, monkeypatch)
    g.calls = g.connect()
    monkeypatch.setattr(workspace, "RETRY_DELAYS_S", (0, 0, 0))
    monkeypatch.setattr(workspace, "POLL_S", 0.02)
    yield g
    g.close()


def test_304_if_none_match_skips_archive_and_cache(gate, monkeypatch) -> None:
    head = gate.head()

    def boom(*_args, **_kwargs):
        raise AssertionError("304 must resolve from Git without building or caching any archive")

    monkeypatch.setattr(gate.appmod, "_stream_published_gzip", boom)
    monkeypatch.setattr(gate.appmod, "_cached_workspace_archive", boom)
    monkeypatch.setattr(gate.appmod, "_workspace_delta", boom)

    resp = gate.client.get(
        "/workspace",
        params={"bundle": "kb-a", "since": head},
        headers={**gate.headers("curator"), "If-None-Match": f'"{head}"'},
    )
    assert resp.status_code == 304
    assert resp.headers["X-AIWiki-Revision"] == head
    assert resp.headers["ETag"] == f'"{head}"'


def test_full_workspace_disk_cache_reuses_and_prunes(gate, monkeypatch) -> None:
    real_stream = gate.appmod._stream_published_gzip
    builds: list[str] = []

    def counted_stream(root, tree, target, paths=gate.appmod._PUBLISHED):
        builds.append(tree)
        return real_stream(root, tree, target, paths)

    monkeypatch.setattr(gate.appmod, "_stream_published_gzip", counted_stream)

    r1 = gate.client.get("/workspace", params={"bundle": "kb-a"}, headers=gate.headers("curator"))
    r2 = gate.client.get("/workspace", params={"bundle": "kb-a"}, headers=gate.headers("curator"))
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content
    assert len(builds) == 1  # second cold pull served straight from disk cache

    # Create 4 more commits and cold-pull each: cache directory must prune to _WORKSPACE_CACHE_KEEP (3).
    for idx in range(4):
        def _edit(other: Path, i=idx) -> None:
            (other / "sources" / f"extra-{i}.source").write_text(f"data {i}\n", encoding="utf-8")

        _push_commit(gate, _edit)
        r = gate.client.get("/workspace", params={"bundle": "kb-a"}, headers=gate.headers("curator"))
        assert r.status_code == 200

    cached = list((gate.writer / ".okf" / "cache").glob("workspace-*.tar.gz"))
    assert len(cached) == gate.appmod._WORKSPACE_CACHE_KEEP


def test_delta_pull_byte_identical_to_full_pull_across_all_scenarios(gate, tmp_path, capsys, monkeypatch) -> None:
    """Delta pull and full pull must produce byte-identical working tree, .ai-wiki/base,
    .mine files, workspace.json, and pull summary across add/mod/del/chmod/CJK/file<->dir swaps."""
    # Seed initial files including file<->dir swap targets and a file to delete.
    rev1 = _push_commit(gate, lambda other: (
        (other / "docs").mkdir(exist_ok=True),
        (other / "docs" / "to-delete.txt").write_text("bye\n", encoding="utf-8"),
        (other / "docs" / "swap-file").write_text("was a file\n", encoding="utf-8"),
        (other / "docs" / "swap-dir").mkdir(exist_ok=True),
        (other / "docs" / "swap-dir" / "leaf.txt").write_text("was inside dir\n", encoding="utf-8"),
        (other / "tools").mkdir(exist_ok=True),
        (other / "tools" / "run.sh").write_text("#!/bin/sh\necho v1\n", encoding="utf-8"),
    ))
    ws_delta = tmp_path / "ws-delta"
    ws_full = tmp_path / "ws-full"
    assert _wiki(capsys, "workspace", "pull", "--dir", ws_delta)[0] == 0
    assert _wiki(capsys, "workspace", "pull", "--dir", ws_full)[0] == 0
    assert json.loads((ws_delta / ".ai-wiki" / "workspace.json").read_text())["base_revision"] == rev1

    # Make identical local edits on both workspaces:
    # - VIEWS: untouched by server -> kept
    # - METRIC: changed by server -> conflict (.mine)
    # - docs/to-delete.txt: deleted by server, locally edited -> conflict (.mine)
    # - tools/run.sh: simulated committed discard with stale .mine -> updated + .mine cleared
    for ws in (ws_delta, ws_full):
        (ws / VIEWS).write_text(gate.read(VIEWS) + "\nLocal kept note.\n", encoding="utf-8")
        (ws / METRIC).write_text(cited(gate.read(METRIC), "My local edit."), encoding="utf-8")
        (ws / "docs" / "to-delete.txt").write_text("local edit on deleted file\n", encoding="utf-8")
        run_bytes = b"#!/bin/sh\necho committed-by-me\n"
        (ws / "tools" / "run.sh").write_bytes(run_bytes)
        (ws / "tools" / "run.sh.mine").write_text("stale conflict\n", encoding="utf-8")
        st_path = ws / ".ai-wiki" / "workspace.json"
        st = json.loads(st_path.read_text(encoding="utf-8"))
        st["discard"] = {"tools/run.sh": hashlib.sha256(run_bytes).hexdigest()}
        st_path.write_text(json.dumps(st, indent=1), encoding="utf-8")

    # Push complex commit: add CJK file, modify METRIC, chmod +x run.sh, delete to-delete.txt,
    # swap-file (file -> dir/child), swap-dir/leaf.txt (dir -> file), plus unpublished viz.html & sources/inbox.
    def _mutate(other: Path) -> None:
        (other / METRIC).write_text(cited(gate.read(METRIC), "Server edit."), encoding="utf-8")
        (other / "docs" / "to-delete.txt").unlink()
        (other / "docs" / "swap-file").unlink()
        (other / "docs" / "swap-file").mkdir()
        (other / "docs" / "swap-file" / "child.txt").write_text("now a dir\n", encoding="utf-8")
        (other / "docs" / "swap-dir" / "leaf.txt").unlink()
        (other / "docs" / "swap-dir").rmdir()
        (other / "docs" / "swap-dir").write_text("now a file\n", encoding="utf-8")
        (other / "docs" / "中文文档.md").write_text("# 中文\n", encoding="utf-8")
        (other / "tools" / "run.sh").write_text("#!/bin/sh\necho v2\n", encoding="utf-8")
        (other / "tools" / "run.sh").chmod(0o755)
        (other / "viz.html").write_text("<html>updated</html>\n", encoding="utf-8")
        (other / "sources" / "inbox").mkdir(parents=True, exist_ok=True)
        (other / "sources" / "inbox" / "drop.source").write_text("secret intake\n", encoding="utf-8")

    rev2 = _push_commit(gate, _mutate)

    # Pull ws_delta using real delta endpoint (verify header was actually delta!).
    seen_modes: list[str | None] = []
    connected = cli._http

    def spy_http(method, route, **kwargs):
        status, headers, body = connected(method, route, **kwargs)
        if route == "/workspace":
            seen_modes.append(headers.get("X-AIWiki-Workspace-Mode"))
        return status, headers, body

    monkeypatch.setattr(cli, "_http", spy_http)
    code_delta, res_delta = _wiki_json(capsys, "workspace", "pull", "--dir", ws_delta)
    assert seen_modes == ["delta"]

    # Pull ws_full simulating an old server that ignores ?since= and sends full archive.
    def old_server_http(method, route, **kwargs):
        if route == "/workspace" and kwargs.get("params"):
            kwargs = {**kwargs, "params": {k: v for k, v in kwargs["params"].items() if k != "since"}}
        return connected(method, route, **kwargs)

    monkeypatch.setattr(cli, "_http", old_server_http)
    code_full, res_full = _wiki_json(capsys, "workspace", "pull", "--dir", ws_full)

    assert code_delta == code_full == 7
    assert res_delta == res_full
    assert res_delta["base_revision"] == rev2
    assert res_delta["kept"] == [VIEWS]
    assert res_delta["conflicts"] == ["docs/to-delete.txt", METRIC]
    assert not (ws_delta / "tools" / "run.sh.mine").exists()
    assert not (ws_delta / "viz.html").exists()
    assert not (ws_delta / "sources" / "inbox" / "drop.source").exists()
    assert _tree_snapshot(ws_delta, skip_meta=True) == _tree_snapshot(ws_full, skip_meta=True)
    assert _tree_snapshot(ws_delta / ".ai-wiki" / "base") == _tree_snapshot(ws_full / ".ai-wiki" / "base")
    assert (ws_delta / ".ai-wiki" / "base" / "tools" / "run.sh").stat().st_mode & 0o111
    assert json.loads((ws_delta / ".ai-wiki" / "workspace.json").read_text()) == json.loads(
        (ws_full / ".ai-wiki" / "workspace.json").read_text()
    )


def test_delta_empty_when_only_unpublished_files_change(gate, tmp_path, capsys) -> None:
    ws = tmp_path / "ws"
    assert _wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 0
    rev1 = json.loads((ws / ".ai-wiki" / "workspace.json").read_text())["base_revision"]

    rev2 = _push_commit(gate, lambda other: (
        (other / "viz.html").write_text("<html>only viz changed</html>\n", encoding="utf-8"),
        (other / "sources" / "inbox").mkdir(parents=True, exist_ok=True),
        (other / "sources" / "inbox" / "x.source").write_text("intake\n", encoding="utf-8"),
    ))

    resp = gate.client.get("/workspace", params={"bundle": "kb-a", "since": rev1}, headers=gate.headers("curator"))
    assert resp.status_code == 200
    assert resp.headers["X-AIWiki-Workspace-Mode"] == "delta"
    assert resp.headers["X-AIWiki-Base-Revision"] == rev1
    assert resp.headers["X-AIWiki-Revision"] == rev2
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tf:
        manifest = json.loads(tf.extractfile("manifest.json").read().decode())
    assert manifest == {"version": 1, "base_revision": rev1, "revision": rev2, "updated": [], "deleted": []}

    code, pulled = _wiki_json(capsys, "workspace", "pull", "--dir", ws)
    assert code == 0 and pulled["base_revision"] == rev2 and pulled["updated"] == [] and pulled["conflicts"] == []


def test_delta_fallbacks_and_malformed_delta_recovery(gate, tmp_path, capsys, monkeypatch) -> None:
    ws = tmp_path / "ws"
    assert _wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 0
    rev1 = json.loads((ws / ".ai-wiki" / "workspace.json").read_text())["base_revision"]

    rev2 = _push_commit(gate, lambda other: (
        (other / "docs").mkdir(exist_ok=True),
        (other / "docs" / "a.txt").write_text("hello world 12345\n", encoding="utf-8"),
        (other / "docs" / "b.txt").write_text("second file 12345\n", encoding="utf-8"),
    ))

    # 1. Short SHA -> full fallback (no delta header)
    r_short = gate.client.get("/workspace", params={"bundle": "kb-a", "since": rev1[:12]}, headers=gate.headers())
    assert r_short.status_code == 200 and "X-AIWiki-Workspace-Mode" not in r_short.headers

    # 2. Bogus non-ancestor SHA -> full fallback
    r_bogus = gate.client.get("/workspace", params={"bundle": "kb-a", "since": "0" * 40}, headers=gate.headers())
    assert r_bogus.status_code == 200 and "X-AIWiki-Workspace-Mode" not in r_bogus.headers

    # 3. Over _DELTA_MAX_PATHS -> full fallback
    monkeypatch.setattr(gate.appmod, "_DELTA_MAX_PATHS", 1)
    r_paths = gate.client.get("/workspace", params={"bundle": "kb-a", "since": rev1}, headers=gate.headers())
    assert r_paths.status_code == 200 and "X-AIWiki-Workspace-Mode" not in r_paths.headers
    monkeypatch.setattr(gate.appmod, "_DELTA_MAX_PATHS", 200)

    # 4. Over _DELTA_MAX_BYTES -> full fallback
    monkeypatch.setattr(gate.appmod, "_DELTA_MAX_BYTES", 5)
    r_bytes = gate.client.get("/workspace", params={"bundle": "kb-a", "since": rev1}, headers=gate.headers())
    assert r_bytes.status_code == 200 and "X-AIWiki-Workspace-Mode" not in r_bytes.headers
    monkeypatch.setattr(gate.appmod, "_DELTA_MAX_BYTES", 32 * 1024 * 1024)

    # 5. Symlink added in commit -> full fallback
    rev3 = _push_commit(gate, lambda other: os.symlink("a.txt", other / "docs" / "link.txt"))
    r_sym = gate.client.get("/workspace", params={"bundle": "kb-a", "since": rev2}, headers=gate.headers())
    assert r_sym.status_code == 200 and "X-AIWiki-Workspace-Mode" not in r_sym.headers

    # 6. Malformed delta from server -> client falls back transparently to full pull without since!
    connected = cli._http
    bad_delta_calls = 0

    def corrupt_delta(method, route, **kwargs):
        nonlocal bad_delta_calls
        if route == "/workspace" and kwargs.get("params", {}).get("since"):
            bad_delta_calls += 1
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w") as tf:
                bad = b'{"version":1,"base_revision":"wrong","revision":"wrong","updated":[],"deleted":[]}'
                info = tarfile.TarInfo("manifest.json")
                info.size = len(bad)
                tf.addfile(info, io.BytesIO(bad))
            return 200, {
                "X-AIWiki-Revision": rev3,
                "X-AIWiki-Workspace-Mode": "delta",
                "X-AIWiki-Base-Revision": rev1,
            }, buf.getvalue()
        return connected(method, route, **kwargs)

    monkeypatch.setattr(cli, "_http", corrupt_delta)
    code, pulled = _wiki_json(capsys, "workspace", "pull", "--dir", ws)
    assert bad_delta_calls == 1
    assert code == 0 and pulled["base_revision"] == rev3
    assert (ws / "docs" / "a.txt").read_text(encoding="utf-8") == "hello world 12345\n"


def test_delta_crash_self_heals_on_next_pull(gate, tmp_path, capsys) -> None:
    ws = tmp_path / "ws"
    assert _wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 0
    # Simulate half-applied delta where .ai-wiki/base diverged from workspace.json before crash.
    (ws / ".ai-wiki" / "base" / METRIC).write_text("partial crash state\n", encoding="utf-8")

    rev2 = _push_commit(gate, lambda other: (
        (other / "docs").mkdir(exist_ok=True),
        (other / "docs" / "healed.txt").write_text("healed\n", encoding="utf-8"),
    ))
    code, pulled = _wiki_json(capsys, "workspace", "pull", "--dir", ws)
    assert code == 0 and pulled["base_revision"] == rev2
    assert (ws / ".ai-wiki" / "base" / METRIC).read_bytes() == (gate.writer / METRIC).read_bytes()
    assert (ws / "docs" / "healed.txt").read_text(encoding="utf-8") == "healed\n"


def test_client_stat_cache_racy_clean_and_reuses_old_files(gate, tmp_path, capsys, monkeypatch) -> None:
    ws = tmp_path / "ws"
    assert _wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 0
    state = workspace.load(ws)

    # Advance logical clock past the 2 s racy-clean window so pulled files become cacheable.
    base_now = time.time_ns() + 10_000_000_000
    monkeypatch.setattr(workspace.time, "time_ns", lambda: base_now)

    # First call populates stat-cache.json.
    changed1, hashes1 = workspace.changes(ws, state)
    assert changed1 == {}
    assert (ws / ".ai-wiki" / "stat-cache.json").is_file()

    # Second call with read_bytes spied: ZERO files should be read from disk!
    reads: list[str] = []
    orig_read_bytes = Path.read_bytes

    def spy_read_bytes(self: Path) -> bytes:
        if self.is_relative_to(ws) and ".ai-wiki" not in self.parts:
            reads.append(self.relative_to(ws).as_posix())
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
    changed2, hashes2 = workspace.changes(ws, state)
    assert changed2 == {} and hashes2 == hashes1 and reads == []

    # Same-length in-place edit while real clock is inside the racy window: MUST be detected immediately!
    monkeypatch.setattr(workspace.time, "time_ns", time.time_ns)
    orig_metric = (ws / METRIC).read_bytes()
    mutated = b"X" + orig_metric[1:]
    assert len(mutated) == len(orig_metric)
    (ws / METRIC).write_bytes(mutated)

    changed3, hashes3 = workspace.changes(ws, state)
    assert changed3 == {METRIC: "modified"}
    assert hashes3[METRIC] == hashlib.sha256(mutated).hexdigest()


def test_validate_then_propose_dedups_local_judge_and_invalidates(gate, tmp_path, capsys, monkeypatch) -> None:
    ws = tmp_path / "ws"
    assert _wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 0
    item_id = gate.item(run="WAIO-900")
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")

    judge_calls: list[int] = []
    real_judge = workspace.judge

    def counted_judge(*args, **kwargs):
        judge_calls.append(1)
        return real_judge(*args, **kwargs)

    monkeypatch.setattr(workspace, "judge", counted_judge)

    # 1. Placeholder validate (no --item / --upload) must NOT populate last-validated cache.
    assert _wiki(capsys, "validate", "--dir", ws)[0] == 0
    assert len(judge_calls) == 1
    assert not (ws / ".ai-wiki" / "last-validated.json").exists()

    # 2. Real validate with --item populates cache; immediate propose skips duplicate local judge
    #    AND its post-commit workspace pull uses incremental delta mode!
    assert _wiki(capsys, "validate", "--dir", ws, "--item", item_id)[0] == 0
    assert len(judge_calls) == 2
    assert (ws / ".ai-wiki" / "last-validated.json").is_file()

    seen_pull_modes: list[str | None] = []
    connected = cli._http

    def spy_pull(method, route, **kwargs):
        status, headers, body = connected(method, route, **kwargs)
        if route == "/workspace":
            seen_pull_modes.append(headers.get("X-AIWiki-Workspace-Mode"))
        return status, headers, body

    monkeypatch.setattr(cli, "_http", spy_pull)
    code, receipt = _wiki_json(capsys, "propose", "--dir", ws, "--item", item_id, "--state-dir", tmp_path / "st")
    monkeypatch.setattr(cli, "_http", connected)
    assert code == 0, receipt
    assert receipt["closed_items"] == [item_id]
    assert len(judge_calls) == 2  # local judge was NOT duplicated!
    assert seen_pull_modes == ["delta"]  # post-commit pull used incremental delta!
    assert not (ws / ".ai-wiki" / "last-validated.json").exists()  # cleared on propose/pull

    # 3. Any 1-byte edit after validate MUST invalidate cache and run local judge (rejecting before POST).
    item2 = gate.item(text=EVIDENCE + b"\nUpdate 2.\n", run="WAIO-900", topic="repo:x#tasks/funnel2")
    (ws / METRIC).write_text(cite(gate.read(METRIC), "funnel-status-2026-09-25", "Second valid claim."),
                             encoding="utf-8")
    assert _wiki(capsys, "validate", "--dir", ws, "--item", item2)[0] == 0
    assert len(judge_calls) == 3

    # 3a. Expired TTL must force re-judge.
    cache_path = ws / ".ai-wiki" / "last-validated.json"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    cached["at"] -= workspace._VALIDATION_TTL_S + 10
    cache_path.write_text(json.dumps(cached), encoding="utf-8")
    req, ev = workspace.build(ws, workspace.load(ws), item=item2)
    assert workspace._has_valid_cache(ws, workspace.load(ws), req, ev) is False

    # Re-validate fresh, then verify changing --allow-retype invalidates cache.
    assert _wiki(capsys, "validate", "--dir", ws, "--item", item2)[0] == 0
    assert len(judge_calls) == 4
    req_allow, ev_allow = workspace.build(ws, workspace.load(ws), item=item2,
                                          allow_retype=[(METRIC, "renamed upstream")])
    assert workspace._has_valid_cache(ws, workspace.load(ws), req_allow, ev_allow) is False

    # Add an uncited line to VIEWS after validate -> fingerprint changes -> local judge runs and rejects!
    (ws / VIEWS).write_text(gate.read(VIEWS) + "\nUncited after validate.\n", encoding="utf-8")
    before_posts = sum(1 for c in gate.calls if c[1] == "/changesets")
    code2, rej = _wiki_json(capsys, "propose", "--dir", ws, "--item", item2, "--state-dir", tmp_path / "st")
    after_posts = sum(1 for c in gate.calls if c[1] == "/changesets")
    assert code2 == 6 and rej["errors"][0]["code"] == "uncited_change"
    assert len(judge_calls) == 5  # local judge ran!
    assert after_posts == before_posts  # rejected locally, never sent!
    assert not (ws / ".ai-wiki" / "last-validated.json").exists()


def test_validate_waits_for_a_delta_pull_updating_the_base(gate, tmp_path, capsys, monkeypatch) -> None:
    """Delta pull holds _locked(root) across its in-place base update so concurrent validate waits."""
    ws = tmp_path / "ws"
    (tmp_path / "status.md").write_bytes(EVIDENCE)
    assert _wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 0
    assert gate.post(gate.request(gate.put(VIEWS, cite(gate.read(VIEWS))))).json()["status"] == "done"
    (ws / METRIC).write_text(cited(gate.read(METRIC)), encoding="utf-8")

    real_replace = workspace.os.replace
    updating, validated = threading.Event(), threading.Event()

    def slow_delta_replace(src, dst, *args, **kwargs):
        real_replace(src, dst, *args, **kwargs)
        if threading.current_thread().name == "delta-pull" and ".ai-wiki/base/" in Path(dst).as_posix():
            updating.set()
            validated.wait(1)

    monkeypatch.setattr(workspace.os, "replace", slow_delta_replace)
    puller = threading.Thread(target=workspace.pull, args=(ws, "kb-a"), name="delta-pull")
    puller.start()
    assert updating.wait(10)
    try:
        code, verdict = _wiki_json(
            capsys, "validate", "--dir", ws, "--upload", tmp_path / "status.md", "--source-id", EVIDENCE_ID
        )
    finally:
        validated.set()
        puller.join(10)

    assert code == 0 and verdict["status"] == "would_apply", verdict
    assert json.loads((ws / ".ai-wiki" / "workspace.json").read_text())["base_revision"] == gate.head()
