"""The read CLI against a real loopback server that times out, resets, drops and answers 5xx/422.

Every failure ends in the standard error (TOON, or one JSON object under --json) with exit 1,
never a traceback; reads retry, writes never do. Backoff is shrunk so the whole file runs in seconds.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from aiwiki.cli import main as cli

SEARCH = {"results": [{"path": "metrics/x.md", "title": "X", "score": 1.0}], "total": 1}
MUTATING = {"detail": "bundle mutation in progress; retry"}
TOP_K_422 = {"detail": [{"type": "less_than_equal", "loc": ["query", "top_k"],
                         "msg": "Input should be less than or equal to 1000", "input": "2000", "ctx": {"le": 1000}}]}


class Server:
    """Scripted answers per route; each request takes the next one and the last one repeats.

    An answer is a dict (200 JSON), a status, ``(status, body)``, ``("sleep", seconds, answer)``,
    ``"reset"`` (RST, no answer) or ``"drop"`` (closed without an answer).
    """

    def __init__(self):
        self.script: dict[str, list] = {}
        self.requests: list[tuple[str, str, dict]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, method):
                parts = urlsplit(self.path)
                if method == "POST":
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                server.requests.append((method, parts.path, dict(parse_qsl(parts.query))))
                queue = server.script.get(parts.path) or [{}]
                action = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(action, tuple) and action[0] == "sleep":
                    time.sleep(action[1])
                    action = action[2]
                if action in ("reset", "drop"):
                    if action == "reset":
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    self.connection.close()
                    self.close_connection = True
                    return
                status, body = (action, {"detail": "unavailable"}) if isinstance(action, int) else (
                    action if isinstance(action, tuple) else (200, action))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.answer("GET")

            def do_POST(self):
                self.answer("POST")

        class Quiet(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # a socket the handler reset on purpose

        self.httpd = Quiet(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def routes(self, route: str) -> list[str]:
        return [method for method, path, _query in self.requests if path == route]


@pytest.fixture
def server(tmp_path, monkeypatch):
    fake = Server()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"endpoint": fake.url, "token": "fixture-token", "bundle": "kb"}))
    monkeypatch.setattr(cli, "CONFIG", config)
    monkeypatch.setattr(cli, "_RETRY_BASE_S", 0.01)
    monkeypatch.setattr(cli, "_RETRY_CAP_S", 0.04)
    monkeypatch.delenv("AIWIKI_RETRY_S", raising=False)
    monkeypatch.delenv("AIWIKI_TIMEOUT", raising=False)
    yield fake
    fake.httpd.shutdown()
    fake.httpd.server_close()


def run(capsys, *args) -> tuple[int, str, str]:
    capsys.readouterr()
    try:
        code = cli.main([str(arg) for arg in args])
    except SystemExit as exit_:
        code = exit_.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- A4: retry reads, never writes -------------------------------------------------------------


@pytest.mark.parametrize("failure", [502, (503, MUTATING), 504, "reset", "drop"])
def test_a_read_rides_out_a_restarting_server(server, capsys, failure) -> None:
    server.script["/search"] = [failure, failure, SEARCH]

    code, out, err = run(capsys, "search", "x")

    assert code == 0 and server.routes("/search") == ["GET"] * 3
    assert out.startswith("count:\n") and '"metrics/x.md"' in out  # stdout is the answer alone
    assert err.count("retrying in") == 2 and "Traceback" not in err


def test_a_read_times_out_then_retries_under_aiwiki_timeout(server, capsys, monkeypatch) -> None:
    monkeypatch.setenv("AIWIKI_TIMEOUT", "0.2")
    server.script["/search"] = [("sleep", 0.6, SEARCH), SEARCH]

    code, out, err = run(capsys, "search", "x")

    assert code == 0 and len(server.routes("/search")) == 2
    assert "timed out; retrying" in err and out.startswith("count:\n")


def test_aiwiki_timeout_bounds_each_attempt(server, capsys, monkeypatch) -> None:
    server.script["/health"] = [("sleep", 1.0, {"bundle": "kb"}), ("sleep", 0.3, {"bundle": "kb"})]
    monkeypatch.setenv("AIWIKI_RETRY_S", "0")
    monkeypatch.setenv("AIWIKI_TIMEOUT", "0.1")
    started = time.monotonic()
    code, out, _err = run(capsys, "health")
    assert code == 1 and time.monotonic() - started < 0.8
    assert 'kind: "error"' in out and "did not answer within 0.1s" in out

    monkeypatch.setenv("AIWIKI_TIMEOUT", "2")
    assert run(capsys, "health")[0] == 0


def test_retry_s_zero_sends_a_read_once(server, capsys, monkeypatch) -> None:
    server.script["/search"] = [502, SEARCH]
    monkeypatch.setenv("AIWIKI_RETRY_S", "0")

    code, out, err = run(capsys, "search", "x")

    assert code == 1 and server.routes("/search") == ["GET"]
    assert "server rejected the request (status 502): unavailable" in out and err == ""


def test_the_retry_budget_runs_out_into_the_standard_error(server, capsys, monkeypatch) -> None:
    server.script["/ls"] = [503]
    monkeypatch.setenv("AIWIKI_RETRY_S", "0.15")
    started = time.monotonic()

    code, out, _err = run(capsys, "ls")

    assert code == 1 and time.monotonic() - started < 1.5
    attempts = len(server.routes("/ls"))
    assert attempts >= 3 and f"(after {attempts} attempts)" in out
    assert out.startswith("error:\n") and '"ai-wiki config show"' not in out


@pytest.mark.parametrize("failure", [502, "reset", ("sleep", 0.4, {"id": "j1"})])
def test_a_write_is_never_sent_twice(server, capsys, monkeypatch, tmp_path, failure) -> None:
    monkeypatch.setenv("AIWIKI_TIMEOUT", "0.2")
    server.script["/ingest"] = [failure, {"id": "j1", "status": "queued"}]
    source = tmp_path / "note.md"
    source.write_text("note")

    code, out, err = run(capsys, "ingest", source)

    assert code == 1 and server.routes("/ingest") == ["POST"]
    assert out.startswith("error:\n") and "retrying" not in err
    assert "the write may have been applied" in out  # the server may have taken it: say so, never resend


# --- A4: every failure is the standard error, JSON under --json ----------------------------------


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    ConnectionResetError(54, "Connection reset by peer"),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    http.client.IncompleteRead(b"{"),
    ssl.SSLError(1, "[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] decryption failed or bad record mac"),
    ssl.SSLCertVerificationError(1, "certificate verify failed"),
    urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
    urllib.error.URLError(TimeoutError("timed out")),
    urllib.error.URLError("unknown url type: htp"),
])
def test_no_network_failure_escapes_as_a_traceback(server, capsys, monkeypatch, exc) -> None:
    """Each ends in the standard error, retried or not (a refused, reset or dropped connection is)."""
    calls = []

    def urlopen(request, timeout=None):
        calls.append(timeout)
        raise exc

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("AIWIKI_RETRY_S", "0.05")

    code, out, _err = run(capsys, "search", "x")
    assert code == 1 and out.startswith("error:\n") and 'kind: "error"' in out
    assert calls[0] == 30  # the default per-request timeout

    code, out, _err = run(capsys, "search", "x", "--json")
    error = json.loads(out)
    assert code == 1 and error["kind"] == "error" and error["help"] == "ai-wiki config show"
    assert error["error"].startswith(("cannot reach the configured ai-wiki server", "the ai-wiki server did not"))


def test_json_errors_are_one_object_like_the_other_json_errors(server, capsys, monkeypatch) -> None:
    server.script["/search"] = [("sleep", 0.4, SEARCH)]
    monkeypatch.setenv("AIWIKI_TIMEOUT", "0.1")
    monkeypatch.setenv("AIWIKI_RETRY_S", "0")

    code, out, _err = run(capsys, "search", "x", "--json")

    assert code == 1 and set(json.loads(out)) == {"error", "kind", "help"}
    code, out, _err = run(capsys, "search", "x", "--stat", "--json")  # a usage error too
    assert code == 2 and json.loads(out)["kind"] == "usage"


def test_a_malformed_timeout_is_a_usage_error(server, capsys, monkeypatch) -> None:
    for value in ("soon", "-1", "0", "nan"):
        monkeypatch.setenv("AIWIKI_TIMEOUT", value)
        code, out, _err = run(capsys, "health")
        assert code == 2 and "$AIWIKI_TIMEOUT must be a number of seconds > 0" in out
    assert server.requests == []


def test_the_installed_command_prints_no_traceback(server, tmp_path) -> None:
    server.script["/search"] = ["reset"]
    server.script["/health"] = [("sleep", 0.6, {})]
    env = {**os.environ, "AIWIKI_CONFIG": str(cli.CONFIG), "AIWIKI_RETRY_S": "0", "AIWIKI_TIMEOUT": "0.2",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    env.pop("AIWIKI_TOKEN", None)
    for args in (["search", "x"], ["health", "--json"]):
        done = subprocess.run([sys.executable, "-m", "aiwiki.cli.main", *args], env=env,
                              capture_output=True, text=True, timeout=30, check=False)
        assert done.returncode == 1 and "Traceback" not in done.stderr, done.stderr
        assert done.stdout.startswith("error:\n") if "--json" not in args else json.loads(done.stdout)["error"]


# --- A5: usage ---------------------------------------------------------------------------------


def test_a_422_shows_the_field_message(server, capsys) -> None:
    server.script["/log"] = [(422, TOP_K_422)]

    code, out, _err = run(capsys, "log")

    assert code == 1 and len(server.routes("/log")) == 1  # a 422 is not retried
    assert "server rejected the request (status 422): top_k: Input should be less than or equal to 1000" in out


def test_top_k_above_the_server_maximum_is_a_usage_error_and_limit_is_top_k(server, capsys) -> None:
    code, out, _err = run(capsys, "search", "x", "--top-k", "2000")
    assert code == 2 and "must be <= 1000, the server's maximum" in out and server.requests == []

    server.script["/search"] = [SEARCH]
    assert run(capsys, "search", "x", "--limit", "3")[0] == 0
    assert run(capsys, "search", "x")[0] == 0
    assert [query["top_k"] for _method, _path, query in server.requests] == ["3", "10"]


@pytest.mark.parametrize("word,first", [
    ("whoami", "ai-wiki config show"), ("status", "ai-wiki health"), ("help", "ai-wiki --help"),
    ("tree", "ai-wiki ls --help"), ("frobnicate", "ai-wiki --help"),
])
def test_an_unknown_root_command_points_at_the_root_help(capsys, word, first) -> None:
    code, out, _err = run(capsys, word)

    assert code == 2 and f"ai-wiki {word} --help" not in out
    assert out.split("help[", 1)[1].splitlines()[1].strip() == json.dumps(first)
    assert '"ai-wiki --help"' in out


def test_grep_accepts_r_and_n_habits(server, capsys) -> None:
    server.script["/grep"] = [{"hits": [{"path": "metrics/x.md", "line": 3, "text": "hit"}]}]

    code, out, _err = run(capsys, "grep", "-rni", "hit")

    assert code == 0 and '"metrics/x.md",3,"hit"' in out


def test_bundle_paths_accept_a_leading_slash_or_dot_slash(server, capsys) -> None:
    server.script["/cat"] = [{"path": "metrics/x.md", "content": "body"}]
    server.script["/links"] = [{"path": "metrics/x.md", "title": "X", "outbound": [], "inbound": []}]
    server.script["/ls"] = [{"items": [{"path": "metrics/x.md", "kind": "concept"}]}]
    server.script["/grep"] = [{"hits": [{"path": "metrics/x.md", "line": 1, "text": "x"}]}]

    for args in (("cat", "/metrics/x.md"), ("cat", "./metrics/x.md"), ("links", "/metrics/x.md"),
                 ("ls", "/metrics"), ("ls", "./metrics/"), ("grep", "x", "/metrics")):
        assert run(capsys, *args)[0] == 0, args
    sent = [query.get("path") or query.get("dir") for _method, _path, query in server.requests]
    assert sent == ["metrics/x.md"] * 3 + ["metrics", "metrics/", "metrics"]
    assert run(capsys, "ls", "/")[0] == 0 and "dir" not in server.requests[-1][2]


def test_a_missing_concept_suggests_the_paths_it_meant(server, capsys) -> None:
    server.script["/cat"] = [(404, {"detail": "not found: features/ai-detector"})]
    server.script["/ls"] = [{"items": [
        {"path": "features/ai-detector.md", "kind": "concept"}, {"path": "risks/ai-detector.md", "kind": "concept"},
        {"path": "features/other.md", "kind": "concept"}, {"path": "SCHEMA.md", "kind": "doc"}]}]

    code, out, _err = run(capsys, "cat", "features/ai-detector")

    assert code == 1 and "did you mean features/ai-detector.md, risks/ai-detector.md?" in out
    assert '"ai-wiki cat features/ai-detector.md"\n  "ai-wiki cat risks/ai-detector.md"' in out
    assert [path for _method, path, _query in server.requests] == ["/cat", "/ls"]  # one extra request
    assert server.requests[-1][2]["recursive"] == "true"

    server.script["/links"] = [(404, {"detail": "not a concept: metrics/other.md"})]
    code, out, _err = run(capsys, "links", "metrics/other.md", "--json")
    assert code == 1 and json.loads(out) == {
        "error": "server rejected the request (status 404): not a concept: metrics/other.md; "
                 "did you mean features/other.md?", "kind": "error", "help": "ai-wiki links features/other.md"}

    server.script["/cat"] = [(404, {"detail": "not found: metrics/none.md"})]
    code, out, _err = run(capsys, "cat", "metrics/none.md")
    assert code == 1 and "did you mean" not in out and '"ai-wiki search \\"none\\""' in out
    code, out, _err = run(capsys, "cat", "features")
    assert code == 1 and '"ai-wiki ls features"' in out
    server.script["/cat"] = [(404, {"detail": "not found: schema.md"})]
    code, out, _err = run(capsys, "cat", "schema.md")  # a case slip still finds the document
    assert code == 1 and '"ai-wiki cat SCHEMA.md"' in out


@pytest.mark.parametrize("verb", ["ls", "grep"])
def test_a_missing_directory_is_an_error_not_an_empty_success(server, capsys, verb) -> None:
    args = ("ls",) if verb == "ls" else ("grep", "x")
    listed = [{"items": []}] if verb == "ls" else []  # ls lists the directory itself first
    server.script["/grep"] = [{"hits": []}]
    server.script["/ls"] = [*listed, {"items": [{"path": "metrics/", "kind": "dir"},
                                                {"path": "SCHEMA.md", "kind": "doc"}]}]

    code, out, _err = run(capsys, *args, "nosuch")
    assert code == 1 and "nosuch: no such directory in this bundle" in out and '"ai-wiki ls"' in out
    assert server.requests[-1][1:] == ("/ls", {"bundle": "kb", "show_all": "true"})  # one parent listing

    server.script["/ls"] = [*listed, {"items": [{"path": "metrics/x.md", "kind": "concept"}]}]
    code, out, _err = run(capsys, *args, "metrics/x.md")
    assert code == 1 and "metrics/x.md: not a directory" in out and '"ai-wiki ls metrics"' in out

    server.script["/ls"] = [*listed, {"items": [{"path": "metrics/", "kind": "dir"}]}]
    code, out, _err = run(capsys, *args, "metrics")  # empty, but a directory: an empty success
    assert code == 0 and out.startswith("count:\n  shown: 0")

    server.script["/ls"] = [{"items": [{"path": "metrics/x.md", "kind": "concept"}]}]
    server.script["/grep"] = [{"hits": [{"path": "metrics/x.md", "line": 1, "text": "x"}]}]
    count = len(server.requests)
    assert run(capsys, *args, "metrics")[0] == 0 and len(server.requests) == count + 1  # no check when found


# --- A6: the cat preview counts sources instead of printing them ------------------------------------


FRONTMATTER = """---
type: Feature
title: AI Detector
tags:
- ai-detector
status: stable
sources:
- id: one
  resource: /sources/one.md.source
  title: One

- id: two
  reference: >-
    folded text
    over lines
  # a comment inside the list
  - nested: list item, not a source
verified:
- {by: process:ai-wiki-adversarial-audit, at: 2026-09-24T05:52:33Z}
---
"""


def test_cat_preview_counts_sources_and_keeps_every_other_key(server, capsys) -> None:
    content = FRONTMATTER + "# AI Detector\n\nBody text.\n" + "x" * 300
    server.script["/cat"] = [{"path": "features/ai-detector.md", "content": content,
                              "metadata": {"status": "stable"}}]

    code, out, _err = run(capsys, "cat", "features/ai-detector.md", "--max-chars", "300")

    assert code == 0
    assert out.startswith(FRONTMATTER.split("sources:")[0]
                          + "sources: <2 entries; run `ai-wiki cat features/ai-detector.md --full` to see them>\n"
                          + "verified:\n- {by: process:ai-wiki-adversarial-audit, at: 2026-09-24T05:52:33Z}\n---\n"
                          + "# AI Detector\n\nBody text.\n")
    assert f"truncated at 300 of {len(content)} chars" in out

    assert run(capsys, "cat", "features/ai-detector.md", "--full")[1] == content
    code, out, _err = run(capsys, "cat", "features/ai-detector.md", "--json")
    assert json.loads(out)["content"] == content


@pytest.mark.parametrize("content,metadata", [
    (FRONTMATTER + "y" * 400, None),  # not a concept (SCHEMA.md, a document): unchanged
    ("---\ntitle: T\nsources: [a, b]\n---\n" + "y" * 400, {"status": "stable"}),  # a flow list: left alone
    ("no frontmatter\nsources:\n- a\n" + "y" * 400, {"status": "stable"}),
], ids=["document", "flow-list", "no-frontmatter"])
def test_cat_preview_leaves_other_shapes_alone(server, capsys, content, metadata) -> None:
    server.script["/cat"] = [{"path": "x.md", "content": content, **({"metadata": metadata} if metadata else {})}]

    code, out, _err = run(capsys, "cat", "x.md", "--max-chars", "120")

    assert code == 0 and out.startswith(content[:120]) and "entries; run" not in out
