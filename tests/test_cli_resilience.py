"""The read CLI against a real loopback server that times out, resets, drops and answers 5xx/422.

Every failure ends in the standard error (TOON, or one JSON object under --json) with exit 1,
never a traceback; reads retry, writes never do. Backoff is shrunk so the whole file runs in seconds.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
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
# What Cloudflare, in front of the production server, answers while the origin restarts: an HTML page.
CLOUDFLARE_HTML = b"<!DOCTYPE html><html><head><title>example.com | 522: Connection timed out</title></head></html>"


class Server:
    """Scripted answers per route; each request takes the next one and the last one repeats.

    An answer is a dict (200 JSON), a status, ``(status, body)`` (bytes are sent as an HTML page),
    ``("sleep", seconds, answer)``, ``"reset"`` (RST, no answer) or ``"drop"`` (closed without an answer).
    """

    def __init__(self):
        self.script: dict[str, list] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.auth: list[str | None] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, method):
                parts = urlsplit(self.path)
                if method == "POST":
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                server.requests.append((method, parts.path, dict(parse_qsl(parts.query))))
                server.auth.append(self.headers.get("Authorization"))
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
                page = isinstance(body, bytes)
                data = body if page else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html" if page else "application/json")
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
    monkeypatch.setattr(cli, "_RETRY_FLOOR_S", 0.05)
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


@pytest.mark.parametrize("failure", [502, (503, MUTATING), 504, "reset", "drop",
                                     *((status, CLOUDFLARE_HTML) for status in (520, 521, 522, 523, 524, 530))])
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


def test_a_cloudflare_page_ends_in_the_standard_error(server, capsys, monkeypatch) -> None:
    """An HTML error page is not JSON: the error names the status alone, in TOON or under --json."""
    server.script["/search"] = [(530, CLOUDFLARE_HTML)]
    monkeypatch.setenv("AIWIKI_RETRY_S", "0")

    code, out, err = run(capsys, "search", "x")
    assert code == 1 and "server rejected the request (status 530)\"" in out and "html" not in out
    assert "Traceback" not in err
    code, out, _err = run(capsys, "search", "x", "--json")
    assert code == 1 and json.loads(out)["error"] == "server rejected the request (status 530)"


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


def test_a_retry_runs_only_for_what_is_left_of_the_window(server, capsys, monkeypatch) -> None:
    """$AIWIKI_RETRY_S counts from the first attempt and cuts a retry's timeout to what is left of
    it, so a hung server costs max($AIWIKI_TIMEOUT, $AIWIKI_RETRY_S), not a whole timeout per retry."""
    server.script["/search"] = [("sleep", 1.5, SEARCH)]
    monkeypatch.setenv("AIWIKI_TIMEOUT", "0.4")
    monkeypatch.setenv("AIWIKI_RETRY_S", "0.6")
    started = time.monotonic()

    code, out, err = run(capsys, "search", "x")

    assert code == 1 and time.monotonic() - started < 1.0  # each retry waiting 0.4 s took 1.2 s
    assert "retrying" in err and "(timed out; $AIWIKI_RETRY_S)" in out  # the last attempt was cut short

    # a timeout longer than the window is not resent: the window is spent when it fires
    monkeypatch.setenv("AIWIKI_TIMEOUT", "0.3")
    monkeypatch.setenv("AIWIKI_RETRY_S", "0.2")
    sent = len(server.routes("/search"))
    code, out, err = run(capsys, "search", "x")
    assert code == 1 and len(server.routes("/search")) == sent + 1 and "retrying" not in err
    assert "did not answer within 0.3s (timed out; $AIWIKI_TIMEOUT)" in out


class TlsServer:
    """HTTPS on loopback whose first ``garbled`` answers break off in a TLS record no key decrypts,
    before the status line (``where="head"``) or inside the body; later ones answer 200."""

    def __init__(self, tmp_path: Path, garbled: int, where: str):
        openssl = shutil.which("openssl")
        if not openssl:
            pytest.skip("needs the openssl command to make a certificate")
        self.cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        subprocess.run([openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-keyout", str(key), "-out", str(self.cert), "-days", "1", "-subj", "/CN=127.0.0.1",
                        "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True, timeout=30)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, key)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"https://127.0.0.1:{self.listener.getsockname()[1]}"
        self.accepted = 0

        def serve():
            while True:
                try:
                    raw, _address = self.listener.accept()
                except OSError:
                    return
                self.accepted += 1
                try:
                    with context.wrap_socket(raw, server_side=True) as conn:
                        conn.recv(65536)
                        if self.accepted > garbled:
                            body = json.dumps({"bundle": "kb"}).encode()
                            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                         b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
                            continue
                        if where == "body":
                            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{")
                        with socket.socket(fileno=os.dup(conn.fileno())) as line:  # under TLS, on the wire
                            line.sendall(b"\x17\x03\x03\x00\x40" + os.urandom(64))
                        time.sleep(0.05)
                except OSError:
                    pass  # the client refused the handshake

        threading.Thread(target=serve, daemon=True).start()


@pytest.mark.parametrize("where", ["head", "body"])
def test_a_read_retries_a_tls_read_error_but_not_a_certificate_it_distrusts(
        server, capsys, monkeypatch, tmp_path, where) -> None:
    tls = TlsServer(tmp_path, garbled=2, where=where)
    cli.CONFIG.write_text(json.dumps({"endpoint": tls.url, "token": "fixture-token"}))
    trusting = ssl.create_default_context(cafile=str(tls.cert))
    opener = cli.urllib.request.build_opener(cli.urllib.request.HTTPSHandler(context=trusting))
    monkeypatch.setattr(cli.urllib.request, "_opener", opener)  # what urlopen sends through
    try:
        code, out, err = run(capsys, "health", "--json")

        assert code == 0 and json.loads(out) and tls.accepted == 3
        assert err.count("retrying in") == 2 and "Traceback" not in err

        monkeypatch.setattr(cli.urllib.request, "_opener", cli.urllib.request.build_opener())  # system trust
        code, out, err = run(capsys, "health", "--json")
        assert code == 1 and tls.accepted == 4 and "retrying" not in err  # setup, not the line: at once
        assert "CERTIFICATE_VERIFY_FAILED" in json.loads(out)["error"]
    finally:
        tls.listener.close()


def test_a_token_is_trimmed_and_never_echoed(server, capsys, monkeypatch) -> None:
    monkeypatch.setenv("AIWIKI_TOKEN", "aiw_secret_TOKEN\r\n")  # read from a CRLF file
    assert run(capsys, "health")[0] == 0 and server.auth == ["Bearer aiw_secret_TOKEN"]

    monkeypatch.setenv("AIWIKI_TOKEN", "aiw_secret\x1bTOKEN")
    code, out, _err = run(capsys, "health", "--json")
    assert code == 1 and "secret" not in out and len(server.auth) == 1
    assert json.loads(out)["error"] == "the token (saved or $AIWIKI_TOKEN) contains an unprintable character"

    def urlopen(request, timeout=None):  # whatever else urllib cannot encode may quote a header
        raise ValueError(f"Invalid header value {request.get_header('Authorization').encode()!r}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("AIWIKI_TOKEN", "aiw_secret_TOKEN")
    code, out, _err = run(capsys, "health", "--json")
    assert code == 1 and "secret" not in out and json.loads(out)["help"] == "ai-wiki config show"


@pytest.mark.parametrize("failure", [502, "reset", ("sleep", 0.4, {"id": "j1"}), (520, CLOUDFLARE_HTML),
                                     (524, CLOUDFLARE_HTML)])
def test_a_write_is_never_sent_twice(server, capsys, monkeypatch, tmp_path, failure) -> None:
    monkeypatch.setenv("AIWIKI_TIMEOUT", "0.2")
    server.script["/ingest"] = [failure, {"id": "j1", "status": "queued"}]
    source = tmp_path / "note.md"
    source.write_text("note")

    code, out, err = run(capsys, "ingest", source)

    assert code == 1 and server.routes("/ingest") == ["POST"]
    assert out.startswith("error:\n") and "retrying" not in err
    assert "the write may have been applied" in out  # the server may have taken it: say so, never resend


@pytest.mark.parametrize("status", [521, 522, 523, 530])
def test_a_write_cloudflare_never_delivered_is_not_resent(server, capsys, tmp_path, status) -> None:
    """Cloudflare could not reach the origin: the write was not applied, and is still sent once."""
    server.script["/ingest"] = [(status, CLOUDFLARE_HTML), {"id": "j1", "status": "queued"}]
    source = tmp_path / "note.md"
    source.write_text("note")

    code, out, err = run(capsys, "ingest", source)

    assert code == 1 and server.routes("/ingest") == ["POST"] and "retrying" not in err
    assert f"server rejected the request (status {status})" in out and "may have been applied" not in out


# --- A4: every failure is the standard error, JSON under --json ----------------------------------


@pytest.mark.parametrize("exc,retried", [
    (TimeoutError("timed out"), True),
    (ConnectionResetError(54, "Connection reset by peer"), True),
    (http.client.RemoteDisconnected("Remote end closed connection without response"), True),
    (http.client.IncompleteRead(b"{"), True),
    # urllib raises what fails while the answer is read bare: any TLS error there is the line's
    (ssl.SSLError(1, "[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] decryption failed or bad record mac"), True),
    (ssl.SSLError(1, "[SSL: RECORD_LAYER_FAILURE] record layer failure"), True),
    (ssl.SSLCertVerificationError(1, "certificate verify failed"), False),
    # ...and wraps what fails while connecting: of the handshake's only a cut stream is retried
    (urllib.error.URLError(ssl.SSLEOFError(8, "EOF occurred in violation of protocol")), True),
    (urllib.error.URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")), False),
    (urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")), False),
    (urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), True),
    (urllib.error.URLError(TimeoutError("timed out")), True),
    (urllib.error.URLError("unknown url type: htp"), False),
])
def test_no_network_failure_escapes_as_a_traceback(server, capsys, monkeypatch, exc, retried) -> None:
    """Each ends in the standard error, after retries for the failures of a restart or a lossy line."""
    calls = []

    def urlopen(request, timeout=None):
        calls.append(timeout)
        raise exc

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("AIWIKI_RETRY_S", "0.05")

    code, out, _err = run(capsys, "search", "x")
    assert code == 1 and out.startswith("error:\n") and 'kind: "error"' in out
    assert calls[0] == 30  # the default per-request timeout
    assert (len(calls) > 1) is retried

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


@pytest.mark.parametrize("flag", ["--j", "--js", "--jso"])
def test_an_abbreviated_json_flag_makes_errors_json_too(server, capsys, monkeypatch, flag) -> None:
    """argparse takes --js for --json: the answer is JSON, and so is the error."""
    server.script["/search"] = [SEARCH, 502]
    monkeypatch.setenv("AIWIKI_RETRY_S", "0")

    code, out, _err = run(capsys, "search", "x", flag)
    assert code == 0 and json.loads(out)["shown"] == 1
    code, out, _err = run(capsys, "search", "x", flag)
    assert code == 1 and json.loads(out)["error"] == "server rejected the request (status 502): unavailable"
    code, out, _err = run(capsys, "search", "x", flag, "--stat")
    assert code == 2 and json.loads(out)["kind"] == "usage"


def test_a_malformed_timeout_is_a_usage_error(server, capsys, monkeypatch) -> None:
    for value in ("soon", "-1", "0", "nan", "1e30", "86401"):  # 1e30 overflows a socket timeout
        monkeypatch.setenv("AIWIKI_TIMEOUT", value)
        code, out, err = run(capsys, "health")
        assert code == 2 and "$AIWIKI_TIMEOUT must be a number of seconds > 0 and <= 86400" in out, value
        assert "Traceback" not in err
    monkeypatch.delenv("AIWIKI_TIMEOUT")
    monkeypatch.setenv("AIWIKI_RETRY_S", "1e30")
    code, out, _err = run(capsys, "health", "--json")
    assert code == 2 and json.loads(out)["error"].startswith("$AIWIKI_RETRY_S must be a number of seconds >= 0 and")
    assert server.requests == []

    monkeypatch.setenv("AIWIKI_TIMEOUT", "86400")
    monkeypatch.setenv("AIWIKI_RETRY_S", "86400")
    assert run(capsys, "health")[0] == 0


def test_doctor_reports_a_non_http_endpoint_in_its_own_result(server, capsys, tmp_path) -> None:
    """doctor (like admin, maint, review, workspace) reports an endpoint it cannot use as a failed check
    and exits 4, as before the scheme check; a read command fails at once with the connect hint."""
    cli.CONFIG.write_text(json.dumps({"endpoint": "ftp://x", "token": "fixture-token"}))

    code, out, _err = run(capsys, "doctor", "--role", "member", "--state-dir", tmp_path / "st", "--json")
    report = json.loads(out)
    assert code == 4 and report["ok"] is False
    checks = {check["check"]: check for check in report["checks"]}
    assert checks["whoami"]["ok"] is False and "'ftp://x' is not an http(s) URL" in checks["whoami"]["detail"]
    assert checks["state_dir"]["ok"] is True

    code, out, _err = run(capsys, "health", "--json")
    assert code == 1 and json.loads(out) == {"error": "the endpoint 'ftp://x' is not an http(s) URL",
                                             "kind": "error", "help": cli.CONNECT_HINT}
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


def test_a_422_whose_loc_is_null_or_a_string_names_the_request(server, capsys) -> None:
    server.script["/log"] = [(422, {"detail": [{"loc": None, "msg": "Field required"},
                                               {"loc": "body", "msg": "Invalid JSON"},
                                               {"loc": ["query"], "msg": "Bad query"}, "stray"]})]

    code, out, err = run(capsys, "log")

    assert code == 1 and "Traceback" not in err
    assert ("server rejected the request (status 422): request: Field required; request: Invalid JSON; "
            "request: Bad query") in out


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


def test_cat_preview_counts_sources_across_column_0_comments(server, capsys) -> None:
    """A column-0 comment inside the list is skipped, not the end of it; one right after it stays shown."""
    frontmatter = ("---\ntitle: T\nsources:\n- id: one\n  title: One\n# a column-0 comment inside\n\n"
                   "- id: two\n  title: Two\n# a column-0 comment after\nverified:\n- {by: x}\n---\n")
    content = frontmatter + "Body.\n" + "x" * 300
    server.script["/cat"] = [{"path": "x.md", "content": content, "metadata": {"status": "stable"}}]

    code, out, _err = run(capsys, "cat", "x.md", "--max-chars", "200")

    assert code == 0 and out.startswith(
        "---\ntitle: T\nsources: <2 entries; run `ai-wiki cat x.md --full` to see them>\n"
        "# a column-0 comment after\nverified:\n- {by: x}\n---\nBody.\n")


@pytest.mark.parametrize("content,metadata", [
    (FRONTMATTER + "y" * 400, None),  # not a concept (SCHEMA.md, a document): unchanged
    ("---\ntitle: T\nsources: [a, b]\n---\n" + "y" * 400, {"status": "stable"}),  # a flow list: left alone
    ("no frontmatter\nsources:\n- a\n" + "y" * 400, {"status": "stable"}),
], ids=["document", "flow-list", "no-frontmatter"])
def test_cat_preview_leaves_other_shapes_alone(server, capsys, content, metadata) -> None:
    server.script["/cat"] = [{"path": "x.md", "content": content, **({"metadata": metadata} if metadata else {})}]

    code, out, _err = run(capsys, "cat", "x.md", "--max-chars", "120")

    assert code == 0 and out.startswith(content[:120]) and "entries; run" not in out
