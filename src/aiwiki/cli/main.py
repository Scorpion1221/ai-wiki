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
$AIWIKI_TOKEN, when set, is used instead of the saved token, so an agent's injected token
never has to be written to the file.
Older configs (a flat {endpoint, token}, or the {current, bundles:{...}} form) are read
and migrated transparently.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
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
_STATE_DIR = Path("~/.ai-wiki/state")  # expanded when used, so help and tests never pin a home


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


def _pair(value: str) -> tuple[str, str]:
    path, _sep, reason = value.partition(":")
    if not path or not reason.strip():
        raise argparse.ArgumentTypeError("expected PATH:REASON")
    return path, reason


def _triple(value: str) -> tuple[str, str, str]:
    path, successor, reason = (value.split(":", 2) + ["", ""])[:3]
    if not path or not successor or not reason.strip():
        raise argparse.ArgumentTypeError("expected PATH:SUPERSEDED_BY:REASON")
    return path, successor, reason


def _run(value: str) -> str:
    if not re.fullmatch(r"[\w.:@/-]{1,128}", value):
        raise argparse.ArgumentTypeError("1-128 of A-Z a-z 0-9 _ . : @ / -")
    return value


def _compatible(service_version: object) -> bool:
    """Client and writer interoperate when their major.minor versions match."""
    def major_minor(value: object) -> list[str] | None:
        parts = str(value or "").split(".")[:2]
        return parts if len(parts) == 2 and all(part.isdigit() for part in parts) else None

    client = major_minor(VERSION)
    return client is not None and client == major_minor(service_version)


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
    depth = 2 if positionals[0] in ("bundle", "config", "workspace", "concept", "admin", "maint") \
        and len(positionals) > 1 else 1
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
    """Coerce historical connection config, preserving optional local agent settings.

    - new form: {endpoint, token, bundle} — passed through.
    - legacy flat: {endpoint, token} — gets bundle=None.
    - old multi-endpoint: {current, bundles:{name:{endpoint,token}}} — those "bundles" were
      really separate servers; we adopt the active one's endpoint+token as the connection.
    """
    agent = {"agent": cfg["agent"]} if "agent" in cfg else {}
    if "endpoint" in cfg:
        return {**agent, "endpoint": cfg.get("endpoint"), "token": cfg.get("token"), "bundle": cfg.get("bundle")}
    if "bundles" in cfg:  # migrate the old multi-endpoint schema
        b = (cfg.get("bundles") or {}).get(cfg.get("current") or "") or {}
        return {**agent, "endpoint": b.get("endpoint"), "token": b.get("token"), "bundle": None}
    return {**agent, "endpoint": None, "token": None, "bundle": None}


def _save(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    CONFIG.chmod(0o600)


def _token(cfg: dict) -> str | None:
    """$AIWIKI_TOKEN wins over the saved token."""
    return os.environ.get("AIWIKI_TOKEN") or cfg.get("token")


def _conn() -> tuple[str, str]:
    cfg = _normalize(_load())
    token = _token(cfg)
    if not cfg.get("endpoint") or not token:
        _fail("not configured: endpoint and token (saved or $AIWIKI_TOKEN) are required",
              help_command='ai-wiki config set --endpoint <url> --token <token>')
    return cfg["endpoint"], token


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


def _http(method: str, route: str, *, bundle: str | None = None, params: dict | None = None,
          data: bytes | None = None, headers: dict | None = None, timeout: float = 60):
    """One request, answered as ``(status, headers, body)`` whatever its status.

    Only a network failure raises (``OSError``: ``URLError``, a timeout, a truncated or
    garbled answer), so a caller can retry it; the returned headers are looked up
    case-insensitively.
    """
    endpoint, token = _conn()
    query = {k: v for k, v in {**(params or {}), "bundle": bundle}.items() if v is not None}
    url = f"{endpoint.rstrip('/')}{route}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    sent = {"Authorization": f"Bearer {token}", "User-Agent": _UA, **(headers or {})}
    if data is not None:
        sent.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=sent, method=method)
    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:  # every non-2xx, a 304 included
            return e.code, e.headers, e.read()
    except http.client.HTTPException as exc:  # IncompleteRead, BadStatusLine: the answer was lost
        raise OSError(f"{type(exc).__name__}: {exc}") from exc


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


def _search_row(result: dict) -> dict:
    match = result.get("match") if isinstance(result.get("match"), dict) else {}
    return {
        "path": result.get("path"),
        "title": result.get("title"),
        "status": result.get("status"),
        "trust": result.get("trust"),
        "freshness": result.get("freshness"),
        "verification_current": result.get("verification_current"),
        "generated_at": result.get("generated_at"),
        "verified_at": result.get("verified_at"),
        "current_verified_at": result.get("current_verified_at"),
        "score": result.get("score"),
        "phrase": bool(match.get("phrase")),
        "coverage": match.get("coverage"),
        "fields": "|".join(map(str, match.get("fields") or [])),
        "terms": "|".join(map(str, match.get("terms") or [])),
        "context": " — ".join(value for value in (
            (result.get("description") or "")[:150], result.get("snippet") or "") if value),
    }


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
    if not cfg.get("endpoint") or not _token(cfg):
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
            "okf_version": health.get("okf_version"),
            "git_revision": health.get("git_revision"),
            "status_counts": health.get("by_status") or {},
            "trust_counts": health.get("by_trust") or {},
            "freshness_counts": health.get("by_freshness") or {},
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
    p_ls.add_argument("--json", action="store_true", help="emit the complete entry array as JSON")
    p_cat = sub.add_parser(
        "cat", help="print a concept; large files are previewed by default", command_path="ai-wiki cat",
        epilog=_examples("ai-wiki cat metrics/subscription-rate.md", "ai-wiki cat SCHEMA.md --full"), **common,
    )
    p_cat.add_argument("path")
    p_cat.add_argument("--json", action="store_true",
                       help="emit content plus derived OKF metadata as JSON")
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
    p_grep.add_argument("--json", action="store_true", help="emit hits and truncation counts as JSON")
    p_search = sub.add_parser(
        "search", help="ranked lexical search (CJK-aware)", command_path="ai-wiki search",
        epilog=_examples("ai-wiki search \"subscription rate\"", "ai-wiki search \"订阅率\" --top-k 20"),
        **common,
    )
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=_positive, default=10, help="maximum results (default: 10)")
    p_search.add_argument("--json", action="store_true", help="emit results and truncation counts as JSON")
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
    p_log.add_argument("--json", action="store_true", help="emit lines and truncation counts as JSON")
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
    p_ing.add_argument("--json", action="store_true", help="emit submission receipts as JSON")
    p_audit = sub.add_parser(
        "audit", help="adversarially review a completed ingest job", command_path="ai-wiki audit",
        epilog=_examples("ai-wiki audit <ingest-job-id>"), **common,
    )
    p_audit.add_argument("ingest_job_id")
    p_audit.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    p_jobs = sub.add_parser(
        "jobs", help="check an ingest or audit job by id", command_path="ai-wiki jobs",
        epilog=_examples("ai-wiki jobs <job-id>"), **common,
    )
    p_jobs.add_argument("job_id", nargs="?")
    p_jobs.add_argument("--pending-audit", action="store_true", help="list successful ingests missing an audit")
    p_jobs.add_argument("--older-than-hours", type=_limit, default=24, help="minimum pending age (default: 24)")
    p_jobs.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    p_maintain = sub.add_parser(
        "maintain", help="resume a durable source manifest through ingest and audit",
        command_path="ai-wiki maintain",
        epilog=_examples(
            "ai-wiki -b my-kb maintain --manifest sources.json --state-dir ~/.ai-wiki/maintenance/my-kb",
            "ai-wiki -b my-kb maintain --state-dir ~/.ai-wiki/maintenance/my-kb --retry-now",
            "ai-wiki maintain --state-dir ~/.ai-wiki/maintenance/my-kb --status",
            "ai-wiki maintain --state-dir ~/.ai-wiki/maintenance/my-kb --drop 1fcfb67c --reason 'superseded by X'",
        ), **common,
    )
    p_maintain.add_argument("--manifest", type=Path, help="add sources; omit to resume saved work only")
    p_maintain.add_argument("--state-dir", required=True, type=Path, help="persistent, single-writer state directory")
    p_maintain.add_argument("--audit-pending", action="store_true",
                            help="also discover orphaned ingests older than 24h")
    p_maintain.add_argument("--retry-now", action="store_true",
                            help="skip retry cooldowns once; never bypasses attempt caps, "
                                 "non-retryable failures, or rollback gates")
    p_maintain.add_argument("--status", action="store_true",
                            help="print the saved state summary offline (no network, no lock)")
    p_maintain.add_argument("--drop", metavar="SHA256_PREFIX",
                            help="abandon one pending/needs_repair source for good (needs --reason); "
                                 "newer versions of its identity stop waiting behind it")
    p_maintain.add_argument("--reason", help="why the --drop source is abandoned (kept in state)")
    p_maintain.add_argument("--import-only", action="store_true",
                            help="freeze manifest sources and import listed job receipts; submit nothing")
    p_maintain.add_argument("--poll-seconds", type=_limit, default=15, help="job poll interval (default: 15)")
    p_maintain.add_argument("--wait-seconds", type=_positive, default=3600,
                            help="maximum wait per stage; timeout preserves job ID (default: 3600)")
    p_maintain.add_argument("--json", action="store_true",
                            help="emit the run summary as JSON (receipts stay in <state-dir>/state.json)")
    from aiwiki.cli import admin, maint

    admin.add_parser(sub, common)
    maint.add_parser(sub, common)

    # curation through changesets (design §3): a local workspace judged by the gate's own code
    p_doctor = sub.add_parser(
        "doctor", help="preflight a role: API contract, exact scopes, bundle, state dir, tools (exit 4 fails)",
        command_path="ai-wiki doctor",
        epilog=_examples("ai-wiki doctor --role curator", "ai-wiki doctor --role auditor --json"), **common,
    )
    p_doctor.add_argument("--role", required=True, choices=("curator", "auditor", "member"))
    p_doctor.add_argument("--state-dir", type=Path, default=_STATE_DIR, help=f"default: {_STATE_DIR}")
    p_doctor.add_argument("--skills-dir", type=Path, help="report the role's installed skills by digest")
    p_doctor.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    p_ws = sub.add_parser(
        "workspace", help="pull the published bundle into a local workspace; show local changes",
        command_path="ai-wiki workspace",
        epilog=_examples("ai-wiki workspace pull --dir ws", "ai-wiki workspace status --dir ws",
                         "ai-wiki workspace diff --dir ws --stamped --item <item>"), **common,
    )
    wsub = p_ws.add_subparsers(dest="action", required=True)
    p_pull = wsub.add_parser(
        "pull", help="fetch the published revision; local edits of paths the server changed become <path>.mine "
                     "(exit 7)", command_path="ai-wiki workspace pull",
        epilog=_examples("ai-wiki -b solvely-wiki workspace pull --dir ws"), **common,
    )
    p_status = wsub.add_parser(
        "status", help="list local changes against the pulled base", command_path="ai-wiki workspace status",
        epilog=_examples("ai-wiki workspace status --dir ws"), **common,
    )
    p_diff = wsub.add_parser(
        "diff", help="unified diff of local changes; --stamped shows the bytes the service would commit",
        command_path="ai-wiki workspace diff",
        epilog=_examples("ai-wiki workspace diff --dir ws", "ai-wiki workspace diff --dir ws --stamped --item <item>"),
        **common,
    )
    p_diff.add_argument("--stamped", action="store_true", help="diff against the service-stamped result")
    p_concept = sub.add_parser(
        "concept", help="scaffold a concept without service-owned keys", command_path="ai-wiki concept",
        epilog=_examples("ai-wiki concept new metrics/x.md --type Metric --title X --description D --tags a,b "
                         "--source-id s"), **common,
    )
    p_new = p_concept.add_subparsers(dest="action", required=True).add_parser(
        "new", help="write the frontmatter skeleton; you write the body", command_path="ai-wiki concept new",
        epilog=_examples('ai-wiki concept new decisions/x.md --type Decision --title "X" --description "…" '
                         "--tags checkout,recovery --source-id x-status"), **common,
    )
    p_new.add_argument("path", help="bundle-relative concept path, e.g. decisions/x.md")
    p_new.add_argument("--type", required=True)
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--description", required=True)
    p_new.add_argument("--tags", required=True, help="comma-separated")
    p_new.add_argument("--source-id", required=True, help="the evidence id the concept cites as evidence:packet")
    p_validate = sub.add_parser(
        "validate", help="judge the workspace's changeset locally with the service gate's code (exit 6 rejects)",
        command_path="ai-wiki validate",
        epilog=_examples("ai-wiki validate --dir ws --item <item>", "ai-wiki validate --dir ws --json"), **common,
    )
    p_propose = sub.add_parser(
        "propose", help="validate, then submit the workspace's changeset to the writer gate",
        command_path="ai-wiki propose",
        epilog=_examples("ai-wiki propose --dir ws --item <item> --run WAIO-612",
                         "ai-wiki propose --dir ws --upload notes.md --source-id notes-2026-09 --dry-run"), **common,
    )
    p_propose.add_argument("--dry-run", action="store_true", help="ask the writer for its verdict; commit nothing")
    p_propose.add_argument("--no-close", action="store_true", help="leave the work item open (more changesets follow)")
    p_propose.add_argument("--wait", type=_positive, default=1800,
                           help="seconds to follow a queued job (default: 1800)")
    p_propose.add_argument("--state-dir", type=Path, default=_STATE_DIR,
                           help=f"the maintenance run's state directory, where it records the proposal "
                                f"(default: {_STATE_DIR})")
    for parser in (p_pull, p_status, p_diff, p_new, p_validate, p_propose):
        parser.add_argument("--dir", default=".", help="the workspace directory (default: .)")
    for parser in (p_diff, p_validate, p_propose):
        evidence = parser.add_mutually_exclusive_group(required=parser is p_propose)
        evidence.add_argument("--item", help="cite the frozen evidence of this work item (it_<id>)")
        evidence.add_argument("--upload", help="cite this file as the evidence packet (needs --source-id)")
        parser.add_argument("--source-id", help="the evidence id the concepts cite (default: the one they cite)")
        parser.add_argument("--deprecate", action="append", type=_triple, metavar="PATH:SUPERSEDED_BY:REASON")
        parser.add_argument("--allow-shrink", action="append", type=_pair, metavar="PATH:REASON",
                            help="let PATH's body shrink below 70%% of its base, for REASON")
        parser.add_argument("--allow-retype", action="append", type=_pair, metavar="PATH:REASON",
                            help="let PATH's type or title change, for REASON")
        parser.add_argument("--run", type=_run, help="the maintenance run (X-AIWiki-Run), e.g. WAIO-612")
    for parser in (p_pull, p_status, p_diff, p_validate, p_propose):
        parser.add_argument("--json", action="store_true", help="emit JSON instead of TOON")

    ap.command_path = _command_path(args)
    a = ap.parse_args(args)

    if a.cmd == "bundle":
        return _cmd_bundle(a)
    if a.cmd == "config":
        return _cmd_config(a)

    bsel = _active(a.bundle)  # bundle to target on the server (None → server default)

    if a.cmd == "doctor":
        from aiwiki.cli import doctor

        result = doctor.run(a.role, bundle=bsel, state_dir=a.state_dir.expanduser(),
                            skills_dir=a.skills_dir.expanduser() if a.skills_dir else None)
        if a.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            emit(object_lines("doctor", {"role": result["role"], "ok": result["ok"]}),
                 table_lines("checks", result["checks"], ("check", "ok", "detail")))
        return 0 if result["ok"] else 4
    if a.cmd in ("workspace", "concept", "validate", "propose"):
        from aiwiki.cli import workspace

        if getattr(a, "upload", None) and not a.source_id:
            _fail("--upload needs --source-id", help_command=f"{ap.command_path} --help", code=2)
        try:
            return workspace.run(a, a.bundle)  # a workspace keeps its bundle; only -b may contradict it
        except workspace.WorkspaceError as exc:
            _fail(str(exc), help_command=f"{ap.command_path} --help", code=exc.code)
        except OSError as exc:  # the writer unreachable, or the workspace unwritable
            _fail(str(exc), help_command="ai-wiki config show")
    if a.cmd == "admin":
        return admin.command(a, bsel)
    if a.cmd == "maint":
        return maint.command(a, a.bundle)  # a run keeps its bundle; only -b may contradict it

    if a.cmd == "maintain":
        from aiwiki.cli import maintain

        if a.status and (a.manifest or a.import_only or a.retry_now or a.audit_pending):
            _fail("--status only reads saved state; drop the other maintenance flags",
                  help_command="ai-wiki maintain --help", code=2)
        if a.drop and (a.status or a.manifest or a.import_only or a.retry_now or a.audit_pending):
            _fail("--drop only changes one saved source; drop the other maintenance flags",
                  help_command="ai-wiki maintain --help", code=2)
        if bool(a.drop) != bool(a.reason):
            _fail("--drop and --reason go together", help_command="ai-wiki maintain --help", code=2)
        if a.import_only and not a.manifest:
            _fail("--import-only needs --manifest", help_command="ai-wiki maintain --help", code=2)
        try:
            if a.status:
                result = maintain.status(a.state_dir.expanduser())
            elif a.drop:
                result = maintain.drop(a.state_dir.expanduser(), a.drop, a.reason)
            else:
                # Resolve a default to a concrete name before binding durable state to it.
                bsel = bsel or _api("/health").get("bundle")
                if not bsel:
                    _fail("maintenance needs a bundle", help_command="ai-wiki bundle use <name>", code=2)
                result = maintain.run(manifest=a.manifest.expanduser() if a.manifest else None,
                                      state_dir=a.state_dir.expanduser(), bundle=bsel,
                                      audit_pending=a.audit_pending, retry_now=a.retry_now,
                                      poll=a.poll_seconds, wait=a.wait_seconds, import_only=a.import_only)
        except (maintain.Pending, ValueError, OSError, KeyError, TypeError) as exc:
            # Pending (another runner holds the lock, the writer is unreachable before any work)
            # resumes on the next run: 1. Usage and state errors are fatal: 2.
            code = 1 if isinstance(exc, maintain.Pending) else 2
            if a.json:
                print(json.dumps({"error": str(exc), "help": "ai-wiki maintain --help"}, ensure_ascii=False))
                return code
            _fail(str(exc), help_command="ai-wiki maintain --help", code=code)
        if a.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            emit(object_lines("count", {k: result[k] for k in maintain.STATUSES}),
                 object_lines("writer_retry", result["writer_retry"]) if result.get("writer_retry") else [],
                 table_lines("warnings", ({"warning": w} for w in result.get("warnings", [])), ("warning",))
                 if result.get("warnings") else [],
                 table_lines("sources", result["sources"], ("identity", "status", "action", "retry_at")))
        return maintain.exit_code(result)
    elif a.cmd == "health":
        d = _api("/health", bundle=bsel)
        if a.json:
            d = {**d, "client_version": VERSION, "compatible": _compatible(d.get("service_version"))}
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            emit(
                object_lines("bundle", {
                    "name": d.get("bundle"),
                    "concepts": d.get("concepts", 0),
                    "okf_version": d.get("okf_version"),
                    "service_version": d.get("service_version"),
                    "git_revision": d.get("git_revision"),
                }),
                table_lines("types", ({"name": name, "count": count}
                                      for name, count in (d.get("by_type") or {}).items()), ("name", "count")),
                table_lines("statuses", ({"name": name, "count": count}
                                         for name, count in (d.get("by_status") or {}).items()), ("name", "count")),
                table_lines("trust", ({"name": name, "count": count}
                                      for name, count in (d.get("by_trust") or {}).items()), ("name", "count")),
                table_lines("freshness", ({"name": name, "count": count}
                                          for name, count in (d.get("by_freshness") or {}).items()), ("name", "count")),
            )
    elif a.cmd == "ls":
        d = _api("/ls", bundle=bsel, dir=a.dir,
                 recursive=("true" if a.recursive else None), show_all=("true" if a.all else None))
        items = d.get("items") or []
        if a.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            concepts = [item for item in items if item.get("kind") == "concept"]
            entries = [item for item in items if item.get("kind") != "concept"]
            groups = [_count_lines(len(items), len(items))]
            if concepts:
                groups.append(table_lines(
                    "concepts",
                    ({
                        "path": item.get("path"),
                        "status": item.get("status"),
                        "trust": item.get("trust"),
                        "freshness": item.get("freshness"),
                        "verification_current": item.get("verification_current"),
                        "generated_at": item.get("generated_at"),
                        "verified_at": item.get("verified_at"),
                        "current_verified_at": item.get("current_verified_at"),
                        "summary": _ls_summary(item),
                    } for item in concepts),
                    ("path", "status", "trust", "freshness", "verification_current",
                     "generated_at", "verified_at", "current_verified_at", "summary"),
                ))
            if entries:
                groups.append(table_lines(
                    "entries",
                    ({
                        "path": item.get("path"),
                        "kind": item.get("kind"),
                        "summary": _ls_summary(item),
                    } for item in entries),
                    ("path", "kind", "summary"),
                ))
            emit(*groups)
    elif a.cmd == "cat":
        document = _api("/cat", bundle=bsel, path=a.path)
        content = document["content"]
        if a.json:
            print(json.dumps(document, ensure_ascii=False, indent=2))
            return 0
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
            print(json.dumps({
                "hits": shown,
                "shown": len(shown),
                "total": len(hits),
                "truncated": len(shown) < len(hits),
            }, ensure_ascii=False, indent=2))
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
        total = d.get("total") if isinstance(d.get("total"), int) else len(results)
        if a.json:
            print(json.dumps({
                "results": results,
                "shown": len(results),
                "total": total,
                "truncated": len(results) < total,
            }, ensure_ascii=False, indent=2))
        else:
            rows = (_search_row(result) for result in results)
            groups = [_count_lines(len(results), total),
                      table_lines(
                          "results", rows,
                          ("path", "title", "status", "trust", "freshness", "verification_current",
                           "generated_at", "verified_at", "current_verified_at", "score", "phrase",
                           "coverage", "fields", "terms", "context"),
                      )]
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
        d = _api("/log", bundle=bsel, tail=a.tail)
        lines = d.get("lines") or []
        total = d.get("total") if isinstance(d.get("total"), int) else len(lines)
        if a.json:
            print(json.dumps({
                "lines": lines,
                "shown": len(lines),
                "total": total,
                "truncated": len(lines) < total,
            }, ensure_ascii=False, indent=2))
        else:
            groups = [
                _count_lines(len(lines), total),
                table_lines("lines", ({"text": line} for line in lines), ("text",)),
            ]
            if len(lines) < total:
                groups.append(table_lines("help", ({
                    "command": f"ai-wiki log --tail {total}",
                    "purpose": f"show all {total} lines",
                },), ("command", "purpose")))
            emit(*groups)
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
            if f == "-":  # pasted text from stdin → stored as raw Markdown evidence
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
            state = f"no-op:{job.get('status')}" if job.get("deduplicated") else (
                job.get("curation") or job.get("status")
            )
            submitted.append({"input": label, "source": job.get("source"), "job": job.get("id"), "state": state})
        if a.json:
            print(json.dumps({"submissions": submitted}, ensure_ascii=False, indent=2))
            return 0
        emit(
            _count_lines(len(submitted), len(submitted)),
            table_lines("submissions", submitted, ("input", "source", "job", "state")),
            table_lines("help", ({"command": "ai-wiki jobs <job-id>", "purpose": "check curation status"},),
                        ("command", "purpose")),
        )
    elif a.cmd == "audit":
        job = _post(f"/jobs/{urllib.parse.quote(a.ingest_job_id, safe='')}/audit", {}, bundle=bsel)
        if a.json:
            print(json.dumps(job, ensure_ascii=False, indent=2))
        else:
            emit(
                object_lines("job", job),
                table_lines("help", ({
                    "command": f"ai-wiki jobs {job.get('id')}",
                    "purpose": "check adversarial audit status",
                },), ("command", "purpose")),
            )
    elif a.cmd == "jobs":
        if bool(a.job_id) == a.pending_audit:
            _fail("provide a job ID or --pending-audit, not both", help_command="ai-wiki jobs --help", code=2)
        job = (_api("/jobs/pending-audit", bundle=bsel, older_than_hours=a.older_than_hours) if a.pending_audit
               else _api(f"/jobs/{a.job_id}", bundle=bsel))
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
        tok = _token(cfg) or ""
        output = {"path": str(CONFIG), "endpoint": cfg.get("endpoint"),
                  "token": (tok[:4] + "…") if tok else None,
                  "token_source": ("env:AIWIKI_TOKEN" if os.environ.get("AIWIKI_TOKEN")
                                   else "config" if tok else None),
                  "bundle": cfg.get("bundle") or "server default"}
        if a.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            emit(object_lines("config", output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
