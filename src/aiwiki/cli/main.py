"""ai-wiki CLI — read/maintain a remote OKF wiki over its service API.

One server (URL + token) hosts many *bundles* (knowledge bases). You configure the
connection once, then list / switch / create bundles that live on that server:

    ai-wiki config set --endpoint https://host/ --token <tok>   # connect to the server
    ai-wiki bundle list                # bundles hosted on the server (* = active)
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
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("AIWIKI_CONFIG", str(Path.home() / ".ai-wiki" / "config.json")))
# A real User-Agent — the urllib default ("Python-urllib/x") trips Cloudflare bot rules (error 1010).
_UA = "ai-wiki-cli/0.0.1 (+https://github.com/Scorpion1221/ai-wiki)"


def _load() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.is_file() else {}


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
        sys.exit("not configured — connect to a server first:\n"
                 "  ai-wiki config set --endpoint <url> --token <tok>")
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
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"connection error ({e.reason}) — is the service running / reachable?")


def _fmt_row(it: dict) -> str:
    kind = it.get("kind")
    if kind == "dir":
        tag = f"{it['concepts']} concepts" if it.get("concepts") else "dir"
        detail = it.get("description") or ""
    elif kind == "doc":
        tag, detail = "doc", it.get("description") or ""
    elif kind == "file":
        tag, detail = f"{it.get('bytes', 0)}B", ""
    else:  # concept
        tag = "/".join(x for x in (it.get("type"), it.get("status")) if x) or "concept"
        when = it.get("source_updated_at") or it.get("timestamp") or ""
        detail = f"{when}  {it.get('title') or ''}".strip()
    return f"  {it['path']:<42} {tag:<14} {detail}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ai-wiki", description="read/maintain a remote OKF wiki")
    # global: one-off override of the active bundle, e.g. `ai-wiki -b other search "…"`
    ap.add_argument("-b", "--bundle", metavar="NAME",
                    help="target this bundle on the server for this command (overrides active)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # bundle management — bundles live on the server; these talk to it (except `use`)
    pb = sub.add_parser("bundle", help="list/switch/create bundles hosted on the server")
    pbsub = pb.add_subparsers(dest="action", required=True)
    pbsub.add_parser("list", help="list bundles hosted on the server (* = active)")
    pbu = pbsub.add_parser("use", help="switch the active bundle (saved locally)")
    pbu.add_argument("name")
    pbc = pbsub.add_parser("create", help="create a new empty bundle on the server")
    pbc.add_argument("name")
    pbr = pbsub.add_parser("rm", help="delete a bundle on the server (asks to confirm)")
    pbr.add_argument("name")
    pbr.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")

    # connection config
    c = sub.add_parser("config", help="show config / set the server endpoint+token")
    csub = c.add_subparsers(dest="action", required=True)
    cset = csub.add_parser("set")
    cset.add_argument("--endpoint")
    cset.add_argument("--token")
    csub.add_parser("show")

    sub.add_parser("health", help="bundle status manifest")
    p_ls = sub.add_parser("ls", help="list a level (like ls); -R recurse, -a hidden")
    p_ls.add_argument("dir", nargs="?")
    p_ls.add_argument("-R", "--recursive", action="store_true", help="recurse, flat (like ls -R)")
    p_ls.add_argument("-a", "--all", action="store_true", help="include dotfiles (like ls -a)")
    p_ls.add_argument("--json", action="store_true")
    p_cat = sub.add_parser("cat", help="print a concept")
    p_cat.add_argument("path")
    p_grep = sub.add_parser("grep", help="regex search across concepts")
    p_grep.add_argument("pattern")
    p_grep.add_argument("dir", nargs="?")
    p_grep.add_argument("--fixed", action="store_true", help="literal search (escape regex metacharacters)")
    p_search = sub.add_parser("search", help="ranked lexical search (CJK-aware)")
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=int, default=10)
    p_search.add_argument("--json", action="store_true")
    p_log = sub.add_parser("log", help="recent change ledger")
    p_log.add_argument("--tail", type=int, default=30)
    p_ing = sub.add_parser("ingest", help="submit a source for curation into the active bundle")
    p_ing.add_argument("file", nargs="?", help="markdown file (omit or '-' to read stdin)")
    p_ing.add_argument("--title", help="optional title for the source")
    p_jobs = sub.add_parser("jobs", help="check an ingest job by id")
    p_jobs.add_argument("job_id")

    a = ap.parse_args(argv)

    if a.cmd == "bundle":
        return _cmd_bundle(a)
    if a.cmd == "config":
        return _cmd_config(a)

    bsel = _active(a.bundle)  # bundle to target on the server (None → server default)

    if a.cmd == "health":
        d = _api("/health", bundle=bsel)
        print(f"bundle: {d['bundle']}  ({d['concepts']} concepts)")
        print(f"by type:   {d['by_type']}")
        print(f"by status: {d['by_status']}")
    elif a.cmd == "ls":
        d = _api("/ls", bundle=bsel, dir=a.dir,
                 recursive=("true" if a.recursive else None), show_all=("true" if a.all else None))
        if a.json:
            print(json.dumps(d["items"], ensure_ascii=False, indent=2))
        else:
            for it in d["items"]:
                print(_fmt_row(it))
            counts: dict[str, int] = {}
            for it in d["items"]:
                counts[it.get("kind")] = counts.get(it.get("kind"), 0) + 1
            print("\n" + (", ".join(f"{counts[k]} {k}(s)" for k in sorted(counts)) or "empty"))
    elif a.cmd == "cat":
        print(_api("/cat", bundle=bsel, path=a.path)["content"], end="")
    elif a.cmd == "grep":
        for h in _api("/grep", bundle=bsel, q=a.pattern, dir=a.dir, fixed=("true" if a.fixed else None))["hits"]:
            print(f"  {h['path']}:{h['line']}: {h['text']}")
    elif a.cmd == "search":
        d = _api("/search", bundle=bsel, q=a.query, top_k=a.top_k)
        if a.json:
            print(json.dumps(d["results"], ensure_ascii=False, indent=2))
        else:
            for r in d["results"]:
                when = r.get("source_updated_at") or r.get("timestamp") or ""
                print(f"  [{r['score']:>3}] {r['path']:<46} {when}  {r.get('title') or ''}")
            if not d["results"]:
                print("  (no matches)")
    elif a.cmd == "log":
        print("\n".join(_api("/log", bundle=bsel, tail=a.tail)["lines"]))
    elif a.cmd == "ingest":
        if not a.file or a.file == "-":
            text = sys.stdin.read()
        else:
            text = Path(a.file).expanduser().read_text(encoding="utf-8")
        job = _post("/ingest", {"text": text, "title": a.title}, bundle=bsel)
        print(f"source: {job['source']}")
        print(f"job:    {job['id']}  (curation: {job.get('curation', '?')})")
        print(f"poll:   ai-wiki jobs {job['id']}")
    elif a.cmd == "jobs":
        print(json.dumps(_api(f"/jobs/{a.job_id}", bundle=bsel), ensure_ascii=False, indent=2))
    return 0


def _cmd_bundle(a) -> int:
    if a.action == "list":
        d = _api("/bundles")
        active = _active()
        default = d.get("default")
        rows = d.get("bundles") or []
        if not rows:
            print("(server hosts no bundles)")
        for it in rows:
            name = it["name"]
            mark = "*" if name == active else " "
            tags = []
            if name == default:
                tags.append("default")
            tag = f"  ({', '.join(tags)})" if tags else ""
            print(f" {mark} {name:<24} {it.get('concepts', 0)} concepts{tag}")
        if active and active not in {it["name"] for it in rows}:
            print(f"\n⚠️  active bundle '{active}' is not hosted here — `ai-wiki bundle use <name>`")
        return 0

    if a.action == "use":
        cfg = _normalize(_load())
        cfg["bundle"] = a.name
        _save(cfg)
        print(f"active bundle → {a.name}")
        return 0

    if a.action == "create":
        d = _post("/bundles", {"name": a.name})
        cfg = _normalize(_load())
        cfg["bundle"] = d["name"]  # switch to the bundle you just made
        _save(cfg)
        print(f"created bundle '{d['name']}' (active)")
        return 0

    if a.action == "rm":
        if not a.yes:
            if not sys.stdin.isatty():
                sys.exit(f"refusing to delete '{a.name}' non-interactively — pass -y/--yes")
            ans = input(f"Delete bundle '{a.name}' on the server (irreversible)? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("aborted")
                return 0
        d = _post(f"/bundles/{urllib.parse.quote(a.name)}", None, method="DELETE")
        cfg = _normalize(_load())
        if cfg.get("bundle") == a.name:
            cfg["bundle"] = None
            _save(cfg)
        print(f"deleted '{d.get('name', a.name)}'" + ("; active bundle cleared" if cfg.get("bundle") is None else ""))
        return 0
    return 0


def _cmd_config(a) -> int:
    cfg = _normalize(_load())
    if a.action == "set":
        if a.endpoint:
            cfg["endpoint"] = a.endpoint
        if a.token:
            cfg["token"] = a.token
        _save(cfg)
        print(f"saved → {CONFIG}")
        print(f"  endpoint: {cfg.get('endpoint') or '(unset)'}")
    else:
        tok = cfg.get("token") or ""
        print(f"config:   {CONFIG}")
        print(f"endpoint: {cfg.get('endpoint') or '(unset)'}")
        print(f"token:    {(tok[:4] + '…') if tok else '(unset)'}")
        print(f"bundle:   {cfg.get('bundle') or '(server default)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
