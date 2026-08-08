"""ai-wiki CLI — read/maintain a remote OKF wiki over its service API.

One server (URL + token) hosts many *bundles* (knowledge bases). You configure the
connection once, then list / switch / create bundles that live on that server:

    ai-wiki config set --endpoint https://host/ --token <tok>   # connect to the server
    ai-wiki bundle list                # bundles hosted on the server (active/default state)
    ai-wiki bundle use solvely-web     # switch the active bundle
    ai-wiki bundle create my-kb        # create a new empty bundle on the server
    ai-wiki health                     # reads the active bundle
    ai-wiki -b other search "<q>"      # one-off: read a different bundle for this command

Config lives at ~/.ai-wiki/config.json (override with $AIWIKI_CONFIG):
    {"endpoint": "https://host/", "token": "<tok>", "bundle": "<active-name>"}
Older configs (a flat {endpoint, token}, or the {current, bundles:{...}} form) are read
and migrated transparently.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from aiwiki.cli.toon import emit, emit_error, object_lines, table_lines
from aiwiki.version import VERSION

CONFIG = Path(os.environ.get("AIWIKI_CONFIG", str(Path.home() / ".ai-wiki" / "config.json")))
# A real User-Agent — the urllib default ("Python-urllib/x") trips Cloudflare bot rules (error 1010).
_UA = f"ai-wiki-cli/{VERSION} (+https://github.com/Scorpion1221/ai-wiki)"
_DESCRIPTION = "Read and maintain a curated OKF knowledge bundle over its service API"
_VERSION_FLAGS = {"-v", "-V", "--version"}
_DEFAULT_CAT_CHARS = 8000
_DEFAULT_GREP_LIMIT = 100


class _AxiParser(argparse.ArgumentParser):
    """Argparse with agent-readable usage failures on stdout."""

    def __init__(self, *args, command_path: str = "ai-wiki", **kwargs):
        self.command_path = command_path
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        emit_error(
            message,
            kind="usage",
            usage=self.format_usage(),
            commands=(f"{self.command_path} --help",),
        )
        raise SystemExit(2)


def _fail(message: str, *, help_command: str | None = None, code: int = 1) -> None:
    emit_error(message, kind="usage" if code == 2 else "error",
               commands=((help_command,) if help_command else ()))
    raise SystemExit(code)


def _examples(*commands: str) -> str:
    return "examples:\n" + "\n".join(f"  {command}" for command in commands)


def _limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0 (0 means no limit)")
    return parsed


def _positive(value: str) -> int:
    parsed = _limit(value)
    if parsed == 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def _command_path(args: list[str]) -> str:
    """Best command path for self-correcting root-level argparse errors."""
    positionals: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in ("-b", "--bundle"):
            skip = True
            continue
        if not arg.startswith("-"):
            positionals.append(arg)
    if not positionals:
        return "ai-wiki"
    depth = 2 if positionals[0] in ("bundle", "config") and len(positionals) > 1 else 1
    return "ai-wiki " + " ".join(positionals[:depth])


def _load() -> dict:
    if not CONFIG.is_file():
        return {}
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _fail(f"cannot read valid JSON config from {CONFIG}", help_command="ai-wiki config set --help")
    if not isinstance(data, dict):
        _fail(f"config must contain a JSON object: {CONFIG}", help_command="ai-wiki config set --help")
    return data


def _normalize(cfg: dict) -> dict:
    """Coerce any historical config into {endpoint, token, bundle}.

    - new form: {endpoint, token, bundle} — passed through.
    - legacy flat: {endpoint, token} — gets bundle=None.
    - old multi-endpoint: {current, bundles:{name:{endpoint,token}}} — those "bundles" were
      really separate servers; we adopt the active one's endpoint+token as the connection.
    """
    if "endpoint" in cfg:
        return {"endpoint": cfg.get("endpoint"), "token": cfg.get("token"), "bundle": cfg.get("bundle")}
    if "bundles" in cfg:  # migrate the old multi-endpoint schema
        b = (cfg.get("bundles") or {}).get(cfg.get("current") or "") or {}
        return {"endpoint": b.get("endpoint"), "token": b.get("token"), "bundle": None}
    return {"endpoint": None, "token": None, "bundle": None}


def _save(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    CONFIG.chmod(0o600)


def _conn() -> tuple[str, str]:
    cfg = _normalize(_load())
    if not cfg.get("endpoint") or not cfg.get("token"):
        _fail("not configured: endpoint and token are required",
              help_command='ai-wiki config set --endpoint <url> --token <token>')
    return cfg["endpoint"], cfg["token"]


def _active(override: str | None = None) -> str | None:
    """The bundle a command targets: -b override, else the saved active bundle, else None
    (let the server pick its default)."""
    return override or _normalize(_load()).get("bundle")


def _api(route: str, *, bundle: str | None = None, **params) -> dict:
    endpoint, token = _conn()
    if bundle is not None:
        params["bundle"] = bundle
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{endpoint.rstrip('/')}{route}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": _UA})
    return _send(req)


def _post(route: str, payload: dict, *, bundle: str | None = None, method: str = "POST") -> dict:
    endpoint, token = _conn()
    url = f"{endpoint.rstrip('/')}{route}"
    if bundle is not None:
        url += "?" + urllib.parse.urlencode({"bundle": bundle})
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": _UA},
        method=method,
    )
    return _send(req)


def _send(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            if not body:
                return {}
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                _fail("server returned an invalid response (expected JSON)")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
            detail = payload.get("detail") if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            detail = None
        message = f"server rejected the request (status {e.code})"
        if isinstance(detail, str) and detail.strip():
            message += f": {detail.strip()}"
        hint = "ai-wiki config show" if e.code == 401 else None
        _fail(message, help_command=hint)
    except urllib.error.URLError:
        _fail("cannot reach the configured ai-wiki server", help_command="ai-wiki config show")


def _executable() -> str:
    raw = shutil.which(sys.argv[0]) or sys.argv[0]
    try:
        resolved = str(Path(raw).expanduser().resolve())
    except OSError:
        resolved = raw
    home = str(Path.home())
    return "~" + resolved[len(home):] if resolved.startswith(home + os.sep) else resolved


def _ls_summary(item: dict) -> str:
    kind = item.get("kind")
    if kind == "dir":
        prefix = f"{item.get('concepts', 0)} concepts"
        detail = item.get("description") or ""
    elif kind == "file":
        prefix, detail = f"{item.get('bytes', 0)}B", ""
    elif kind == "doc":
        prefix, detail = "document", item.get("description") or ""
    else:
        prefix = "/".join(value for value in (item.get("type"), item.get("status")) if value) or "concept"
        detail = item.get("title") or ""
    return f"{prefix} — {detail}" if detail else prefix


def _count_lines(shown: int, total: int | None = None) -> list[str]:
    values = {"shown": shown}
    if total is not None:
        values["total"] = total
        values["truncated"] = shown < total
    return object_lines("count", values)


def _home() -> int:
    identity = object_lines(None, {"bin": _executable(), "description": _DESCRIPTION})
    print("\n".join(identity))
    cfg = _normalize(_load())
    if not cfg.get("endpoint") or not cfg.get("token"):
        print()
        emit(
            object_lines("connection", {"configured": False}),
            table_lines("help", ({
                "command": "ai-wiki config set --endpoint <url> --token <token>",
                "purpose": "connect to a server",
            },), ("command", "purpose")),
        )
        return 0

    bundle = _active()
    health = _api("/health", bundle=bundle)
    listing = _api("/ls", bundle=bundle)
    directories = [item for item in listing.get("items", []) if item.get("kind") == "dir"]
    print()
    emit(
        object_lines("bundle", {
            "name": health.get("bundle"),
            "concepts": health.get("concepts", 0),
            "status_counts": health.get("by_status") or {},
        }),
        table_lines("directories", directories, ("path", "concepts")),
        table_lines("help", (
            {"command": "ai-wiki cat SCHEMA.md", "purpose": "read conventions"},
            {"command": "ai-wiki search \"<query>\"", "purpose": "find concepts"},
            {"command": "ai-wiki ls <dir>", "purpose": "browse a directory"},
        ), ("command", "purpose")),
    )
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 1 and args[0] in _VERSION_FLAGS:
        print(VERSION)
        return 0
    if not args:
        return _home()

    common = {"formatter_class": argparse.RawDescriptionHelpFormatter}
    ap = _AxiParser(
        prog="ai-wiki",
        description=_DESCRIPTION,
        epilog=_examples(
            "ai-wiki",
            "ai-wiki search \"subscription rate\"",
            "ai-wiki -b other cat SCHEMA.md",
        ),
        **common,
    )
    # global: one-off override of the active bundle, e.g. `ai-wiki -b other search "…"`
    ap.add_argument("-b", "--bundle", metavar="NAME",
                    help="target this bundle on the server for this command (overrides active)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # bundle management — bundles live on the server; these talk to it (except `use`)
    pb = sub.add_parser(
        "bundle", help="list/switch/create bundles hosted on the server",
        command_path="ai-wiki bundle",
        epilog=_examples("ai-wiki bundle list", "ai-wiki bundle use <name>"),
        **common,
    )
    pbsub = pb.add_subparsers(dest="action", required=True)
    pbl = pbsub.add_parser(
        "list", help="list bundles hosted on the server",
        command_path="ai-wiki bundle list",
        epilog=_examples("ai-wiki bundle list"), **common,
    )
    pbl.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    pbu = pbsub.add_parser(
        "use", help="switch the active bundle (saved locally)",
        command_path="ai-wiki bundle use",
        epilog=_examples("ai-wiki bundle use solvely-web"), **common,
    )
    pbu.add_argument("name")
    pbc = pbsub.add_parser(
        "create", help="create a new empty bundle on the server",
        command_path="ai-wiki bundle create",
        epilog=_examples("ai-wiki bundle create my-kb"), **common,
    )
    pbc.add_argument("name")
    pbr = pbsub.add_parser(
        "rm", help="delete a bundle on the server (requires --yes)",
        command_path="ai-wiki bundle rm",
        epilog=_examples("ai-wiki bundle rm old-kb --yes"), **common,
    )
    pbr.add_argument("name")
    pbr.add_argument("-y", "--yes", action="store_true", help="confirm irreversible deletion (required)")

    # connection config
    c = sub.add_parser(
        "config", help="show config / set the server endpoint+token",
        command_path="ai-wiki config",
        epilog=_examples("ai-wiki config show", "ai-wiki config set --endpoint <url> --token <token>"),
        **common,
    )
    csub = c.add_subparsers(dest="action", required=True)
    cset = csub.add_parser(
        "set", help="save an endpoint and/or token",
        command_path="ai-wiki config set",
        epilog=_examples(
            "ai-wiki config set --endpoint https://wiki.example.com --token <token>",
            "ai-wiki config set --token <new-token>",
        ), **common,
    )
    cset.add_argument("--endpoint")
    cset.add_argument("--token")
    cshow = csub.add_parser(
        "show", help="show the current connection with token redacted",
        command_path="ai-wiki config show",
        epilog=_examples("ai-wiki config show"), **common,
    )
    cshow.add_argument("--json", action="store_true", help="emit JSON instead of TOON")

    p_health = sub.add_parser(
        "health", help="bundle status manifest", command_path="ai-wiki health",
        epilog=_examples("ai-wiki health", "ai-wiki -b other health"), **common,
    )
    p_health.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    p_ls = sub.add_parser(
        "ls", help="list a level (like ls); -R recurse, -a hidden", command_path="ai-wiki ls",
        epilog=_examples("ai-wiki ls", "ai-wiki ls metrics", "ai-wiki ls -R -a"), **common,
    )
    p_ls.add_argument("dir", nargs="?")
    p_ls.add_argument("-R", "--recursive", action="store_true", help="recurse, flat (like ls -R)")
    p_ls.add_argument("-a", "--all", action="store_true", help="include dotfiles (like ls -a)")
    p_ls.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    p_cat = sub.add_parser(
        "cat", help="print a concept; large files are previewed by default", command_path="ai-wiki cat",
        epilog=_examples("ai-wiki cat metrics/subscription-rate.md", "ai-wiki cat SCHEMA.md --full"), **common,
    )
    p_cat.add_argument("path")
    cat_size = p_cat.add_mutually_exclusive_group()
    cat_size.add_argument("--full", action="store_true", help="print complete content")
    cat_size.add_argument("--max-chars", type=_positive, default=_DEFAULT_CAT_CHARS,
                          help=f"preview size (default: {_DEFAULT_CAT_CHARS})")
    p_grep = sub.add_parser(
        "grep", help="regex search across concepts", command_path="ai-wiki grep",
        epilog=_examples("ai-wiki grep \"subscription.*rate\"", "ai-wiki grep \"metrics/x.md\" --fixed"),
        **common,
    )
    p_grep.add_argument("pattern")
    p_grep.add_argument("dir", nargs="?")
    p_grep.add_argument("--fixed", action="store_true", help="literal search (escape regex metacharacters)")
    p_grep.add_argument("--limit", type=_limit, default=_DEFAULT_GREP_LIMIT,
                        help=f"maximum hits to print; 0 means all (default: {_DEFAULT_GREP_LIMIT})")
    p_grep.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    p_search = sub.add_parser(
        "search", help="ranked lexical search (CJK-aware)", command_path="ai-wiki search",
        epilog=_examples("ai-wiki search \"subscription rate\"", "ai-wiki search \"订阅率\" --top-k 20"),
        **common,
    )
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=_positive, default=10, help="maximum results (default: 10)")
    p_search.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    p_links = sub.add_parser(
        "links", help="link graph of a concept: outbound + inbound (backlinks)", command_path="ai-wiki links",
        epilog=_examples("ai-wiki links metrics/subscription-rate.md"), **common,
    )
    p_links.add_argument("path")
    p_links.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    p_log = sub.add_parser(
        "log", help="recent change ledger", command_path="ai-wiki log",
        epilog=_examples("ai-wiki log", "ai-wiki log --tail 100"), **common,
    )
    p_log.add_argument("--tail", type=_limit, default=30, help="number of lines; 0 returns none (default: 30)")
    p_log.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    p_ing = sub.add_parser(
        "ingest", help="submit source(s) for curation into the active bundle", command_path="ai-wiki ingest",
        epilog=_examples(
            "ai-wiki ingest notes.md",
            "ai-wiki ingest report.pdf chart.png",
            "cat notes.md | ai-wiki ingest - --title \"Research notes\"",
        ), **common,
    )
    p_ing.add_argument("files", nargs="*", help="markdown file(s); omit or '-' to read stdin")
    p_ing.add_argument("--title", help="title for the source (requires exactly one input)")
    p_jobs = sub.add_parser(
        "jobs", help="check an ingest job by id", command_path="ai-wiki jobs",
        epilog=_examples("ai-wiki jobs <job-id>"), **common,
    )
    p_jobs.add_argument("job_id")
    p_jobs.add_argument("--json", action="store_true", help="emit JSON instead of TOON")

    ap.command_path = _command_path(args)
    a = ap.parse_args(args)

    if a.cmd == "bundle":
        return _cmd_bundle(a)
    if a.cmd == "config":
        return _cmd_config(a)

    bsel = _active(a.bundle)  # bundle to target on the server (None → server default)

    if a.cmd == "health":
        d = _api("/health", bundle=bsel)
        if a.json:
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            emit(
                object_lines("bundle", {"name": d.get("bundle"), "concepts": d.get("concepts", 0)}),
                table_lines("types", ({"name": name, "count": count}
                                      for name, count in (d.get("by_type") or {}).items()), ("name", "count")),
                table_lines("statuses", ({"name": name, "count": count}
                                         for name, count in (d.get("by_status") or {}).items()), ("name", "count")),
            )
    elif a.cmd == "ls":
        d = _api("/ls", bundle=bsel, dir=a.dir,
                 recursive=("true" if a.recursive else None), show_all=("true" if a.all else None))
        items = d.get("items") or []
        if a.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            rows = ({"path": item.get("path"), "kind": item.get("kind"), "summary": _ls_summary(item)}
                    for item in items)
            emit(_count_lines(len(items), len(items)), table_lines("items", rows, ("path", "kind", "summary")))
    elif a.cmd == "cat":
        content = _api("/cat", bundle=bsel, path=a.path)["content"]
        if a.full or len(content) <= a.max_chars:
            print(content, end="")
        else:
            preview = content[:a.max_chars]
            print(preview, end="" if preview.endswith("\n") else "\n")
            print(f"\n<!-- ai-wiki: truncated at {a.max_chars} of {len(content)} chars; "
                  f"run `ai-wiki cat {a.path} --full` -->")
    elif a.cmd == "grep":
        hits = _api("/grep", bundle=bsel, q=a.pattern, dir=a.dir,
                    fixed=("true" if a.fixed else None)).get("hits") or []
        shown = hits if a.limit == 0 else hits[:a.limit]
        if a.json:
            print(json.dumps(shown, ensure_ascii=False, indent=2))
        else:
            groups = [_count_lines(len(shown), len(hits)), table_lines("hits", shown, ("path", "line", "text"))]
            if len(shown) < len(hits):
                groups.append(table_lines("help", ({
                    "command": f'ai-wiki grep {json.dumps(a.pattern, ensure_ascii=False)} --limit 0',
                    "purpose": f"show all {len(hits)} hits",
                },), ("command", "purpose")))
            emit(*groups)
    elif a.cmd == "search":
        d = _api("/search", bundle=bsel, q=a.query, top_k=a.top_k)
        results = d.get("results") or []
        if a.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            rows = ({
                "path": result.get("path"),
                "title": result.get("title"),
                "status": result.get("status"),
                "score": result.get("score"),
                "context": " — ".join(value for value in (
                    (result.get("description") or "")[:150], result.get("snippet") or "") if value),
            } for result in results)
            total = d.get("total")
            groups = [_count_lines(len(results), total),
                      table_lines("results", rows, ("path", "title", "status", "score", "context"))]
            if isinstance(total, int) and len(results) < total:
                groups.append(table_lines("help", ({
                    "command": f'ai-wiki search {json.dumps(a.query, ensure_ascii=False)} --top-k {total}',
                    "purpose": f"show all {total} matches",
                },), ("command", "purpose")))
            emit(*groups)
    elif a.cmd == "links":
        d = _api("/links", bundle=bsel, path=a.path)
        if a.json:
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            emit(
                object_lines("concept", {"path": d.get("path"), "title": d.get("title")}),
                table_lines("outbound", d.get("outbound") or [], ("path", "title", "type")),
                table_lines("inbound", d.get("inbound") or [], ("path", "title", "type")),
            )
    elif a.cmd == "log":
        lines = _api("/log", bundle=bsel, tail=a.tail).get("lines") or []
        if a.json:
            print(json.dumps(lines, ensure_ascii=False, indent=2))
        else:
            emit(_count_lines(len(lines), len(lines)),
                 table_lines("lines", ({"text": line} for line in lines), ("text",)))
    elif a.cmd == "ingest":
        if not a.files and sys.stdin.isatty():
            _fail("no input provided: pass a file or pipe text on stdin",
                  help_command="ai-wiki ingest <file>", code=2)
        files = a.files or ["-"]
        if a.title and len(files) != 1:
            _fail("--title requires exactly one input",
                  help_command='ai-wiki ingest <file> --title "<title>"', code=2)
        submitted = []
        for f in files:
            single = len(files) == 1
            if f == "-":  # pasted text from stdin → stored as .md
                payload = {"text": sys.stdin.read(), "title": a.title if single else None}
            else:  # any file: ship raw bytes base64 so binaries (pdf/image/…) survive intact
                p = Path(f).expanduser()
                try:
                    content = p.read_bytes()
                except OSError as exc:
                    _fail(f"cannot read input file {p}: {exc.strerror or 'read failed'}",
                          help_command="ai-wiki ingest <readable-file>", code=2)
                payload = {"content_b64": base64.b64encode(content).decode("ascii"),
                           "filename": p.name, "title": a.title if (a.title and single) else None}
            job = _post("/ingest", payload, bundle=bsel)
            label = f if f != "-" else "(stdin)"
            submitted.append({"input": label, "source": job.get("source"), "job": job.get("id"),
                              "curation": job.get("curation") or job.get("status")})
        emit(
            _count_lines(len(submitted), len(submitted)),
            table_lines("submissions", submitted, ("input", "source", "job", "curation")),
            table_lines("help", ({"command": "ai-wiki jobs <job-id>", "purpose": "check curation status"},),
                        ("command", "purpose")),
        )
    elif a.cmd == "jobs":
        job = _api(f"/jobs/{a.job_id}", bundle=bsel)
        if a.json:
            print(json.dumps(job, ensure_ascii=False, indent=2))
        else:
            emit(object_lines("job", job))
    return 0


def _cmd_bundle(a) -> int:
    if a.action == "list":
        d = _api("/bundles")
        active = _active()
        default = d.get("default")
        rows = d.get("bundles") or []
        if a.json:
            print(json.dumps(d, ensure_ascii=False, indent=2))
            return 0
        output = []
        for it in rows:
            name = it["name"]
            state = ",".join(label for label, enabled in (("active", name == active), ("default", name == default))
                             if enabled) or "available"
            output.append({"name": name, "concepts": it.get("concepts", 0), "state": state})
        groups = [_count_lines(len(output), len(output)), table_lines("bundles", output, ("name", "concepts", "state"))]
        if active and active not in {it["name"] for it in rows}:
            groups.append(object_lines("warning", {
                "message": f"active bundle {active!r} is not hosted by this server",
                "command": "ai-wiki bundle use <name>",
            }))
        elif not rows:
            groups.append(object_lines("empty", {"message": "server hosts no bundles"}))
        emit(*groups)
        return 0

    if a.action == "use":
        cfg = _normalize(_load())
        unchanged = cfg.get("bundle") == a.name
        cfg["bundle"] = a.name
        _save(cfg)
        emit(object_lines("bundle", {"name": a.name, "active": True,
                                     "changed": not unchanged, "no_op": unchanged}))
        return 0

    if a.action == "create":
        d = _post("/bundles", {"name": a.name})
        cfg = _normalize(_load())
        cfg["bundle"] = d["name"]  # switch to the bundle you just made
        _save(cfg)
        emit(object_lines("bundle", {"name": d["name"], "created": True, "active": True}))
        return 0

    if a.action == "rm":
        if not a.yes:
            _fail(f"deletion not confirmed for bundle {a.name!r}: --yes is required",
                  help_command=f"ai-wiki bundle rm {a.name} --yes", code=2)
        d = _post(f"/bundles/{urllib.parse.quote(a.name)}", None, method="DELETE")
        cfg = _normalize(_load())
        cleared = cfg.get("bundle") == a.name
        if cleared:
            cfg["bundle"] = None
            _save(cfg)
        emit(object_lines("bundle", {"name": d.get("name", a.name), "deleted": True,
                                     "active_cleared": cleared}))
        return 0
    return 0


def _cmd_config(a) -> int:
    cfg = _normalize(_load())
    if a.action == "set":
        if not a.endpoint and not a.token:
            _fail("provide --endpoint and/or --token",
                  help_command="ai-wiki config set --endpoint <url> --token <token>", code=2)
        if a.endpoint:
            cfg["endpoint"] = a.endpoint
        if a.token:
            cfg["token"] = a.token
        _save(cfg)
        emit(object_lines("config", {"path": str(CONFIG), "saved": True,
                                     "endpoint": cfg.get("endpoint"),
                                     "token_set": bool(cfg.get("token")),
                                     "bundle": cfg.get("bundle") or "server default"}))
    else:
        tok = cfg.get("token") or ""
        output = {"path": str(CONFIG), "endpoint": cfg.get("endpoint"),
                  "token": (tok[:4] + "…") if tok else None,
                  "bundle": cfg.get("bundle") or "server default"}
        if a.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            emit(object_lines("config", output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
