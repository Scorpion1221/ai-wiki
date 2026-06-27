"""ai-wiki CLI — read the wiki over the service API like a remote filesystem.

    ai-wiki config set --endpoint http://127.0.0.1:8787 --token <tok>
    ai-wiki health
    ai-wiki ls [dir]
    ai-wiki cat <path>
    ai-wiki grep <pattern> [dir]
    ai-wiki search <query> [--top-k N]
    ai-wiki log [--tail N]

Config lives at ~/.config/ai-wiki/config.json (override with $AIWIKI_CONFIG).
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


def _save(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    CONFIG.chmod(0o600)


def _api(route: str, **params) -> dict:
    cfg = _load()
    endpoint, token = cfg.get("endpoint"), cfg.get("token")
    if not endpoint or not token:
        sys.exit("not configured — run: ai-wiki config set --endpoint <url> --token <tok>")
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{endpoint.rstrip('/')}{route}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"connection error ({e.reason}) — is the service running?")


def _post(route: str, payload: dict) -> dict:
    cfg = _load()
    endpoint, token = cfg.get("endpoint"), cfg.get("token")
    if not endpoint or not token:
        sys.exit("not configured — run: ai-wiki config set --endpoint <url> --token <tok>")
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}{route}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": _UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"connection error ({e.reason}) — is the service running?")


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
    ap = argparse.ArgumentParser(prog="ai-wiki", description="read the ai-wiki over its service API")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", help="get/set client config")
    csub = c.add_subparsers(dest="action", required=True)
    cset = csub.add_parser("set")
    cset.add_argument("--endpoint")
    cset.add_argument("--token")
    csub.add_parser("show")

    sub.add_parser("health", help="bundle status manifest")
    p_ls = sub.add_parser("ls", help="list a level like shell ls (all entries); -R recurse, -a show hidden")
    p_ls.add_argument("dir", nargs="?")
    p_ls.add_argument("-R", "--recursive", action="store_true", help="recurse, flat (like ls -R)")
    p_ls.add_argument("-a", "--all", action="store_true", help="include dotfiles (like ls -a)")
    p_ls.add_argument("--json", action="store_true")
    p_cat = sub.add_parser("cat", help="print a concept")
    p_cat.add_argument("path")
    p_grep = sub.add_parser("grep", help="regex search across concepts")
    p_grep.add_argument("pattern")
    p_grep.add_argument("dir", nargs="?")
    p_grep.add_argument("--fixed", action="store_true",
                        help="literal search (escape regex metacharacters, e.g. paths)")
    p_search = sub.add_parser("search", help="ranked lexical search (CJK-aware)")
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=int, default=10)
    p_search.add_argument("--json", action="store_true")
    p_log = sub.add_parser("log", help="recent change ledger")
    p_log.add_argument("--tail", type=int, default=30)
    p_ing = sub.add_parser("ingest", help="submit a markdown source; a curation agent folds it into the wiki")
    p_ing.add_argument("file", nargs="?", help="markdown file (omit or '-' to read stdin)")
    p_ing.add_argument("--title", help="optional title for the source")
    p_jobs = sub.add_parser("jobs", help="check an ingest job by id")
    p_jobs.add_argument("job_id")

    a = ap.parse_args(argv)

    if a.cmd == "config":
        if a.action == "set":
            cfg = _load()
            if a.endpoint:
                cfg["endpoint"] = a.endpoint
            if a.token:
                cfg["token"] = a.token
            _save(cfg)
            print(f"saved → {CONFIG}")
        else:
            cfg = _load()
            tok = cfg.get("token")
            print(f"endpoint: {cfg.get('endpoint')}")
            print(f"token:    {'set (' + tok[:4] + '…)' if tok else 'unset'}")
        return 0

    if a.cmd == "health":
        d = _api("/health")
        print(f"bundle: {d['bundle']}  ({d['concepts']} concepts)")
        print(f"by type:   {d['by_type']}")
        print(f"by status: {d['by_status']}")
    elif a.cmd == "ls":
        d = _api("/ls", dir=a.dir, recursive=("true" if a.recursive else None),
                 show_all=("true" if a.all else None))
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
        print(_api("/cat", path=a.path)["content"], end="")
    elif a.cmd == "grep":
        for h in _api("/grep", q=a.pattern, dir=a.dir, fixed=("true" if a.fixed else None))["hits"]:
            print(f"  {h['path']}:{h['line']}: {h['text']}")
    elif a.cmd == "search":
        d = _api("/search", q=a.query, top_k=a.top_k)
        if a.json:
            print(json.dumps(d["results"], ensure_ascii=False, indent=2))
        else:
            for r in d["results"]:
                when = r.get("source_updated_at") or r.get("timestamp") or ""
                print(f"  [{r['score']:>3}] {r['path']:<46} {when}  {r.get('title') or ''}")
            if not d["results"]:
                print("  (no matches)")
    elif a.cmd == "log":
        print("\n".join(_api("/log", tail=a.tail)["lines"]))
    elif a.cmd == "ingest":
        if not a.file or a.file == "-":
            text = sys.stdin.read()
        else:
            text = Path(a.file).expanduser().read_text(encoding="utf-8")
        job = _post("/ingest", {"text": text, "title": a.title})
        print(f"source: {job['source']}")
        print(f"job:    {job['id']}  (curation: {job.get('curation', '?')})")
        print(f"poll:   ai-wiki jobs {job['id']}")
    elif a.cmd == "jobs":
        print(json.dumps(_api(f"/jobs/{a.job_id}"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
