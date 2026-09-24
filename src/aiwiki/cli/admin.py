"""Owner verbs for incident response and migration (design §3, §8.5, §9): ``ai-wiki admin …``.

    changesets      which changesets a principal committed, and the revert that undid each
    revert          revert changesets on the writer, newest first, stopping at the first conflict
    compare         side-by-side diff of what a live and a shadow bundle changed since a base
    cursor import   restore collector cursors from a maintenance report after writer disk loss

Only the owner runs these. They talk HTTP to the writer and never touch Git.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import re
import secrets
import tarfile
import time
import unicodedata
import urllib.parse
from itertools import zip_longest
from pathlib import Path

from aiwiki.cli import main as cli
from aiwiki.cli.toon import emit, object_lines, table_lines

POLL_S = 5.0
# Exit codes (design §3): 6 rejected by the gate, 7 a conflict someone has to resolve.
REJECTED, CONFLICT = 6, 7


def add_parser(sub, common: dict) -> None:
    """Register ``ai-wiki admin`` and its verbs on the root parser's subcommands."""
    admin = sub.add_parser(
        "admin", help="owner verbs: changesets, revert, compare, cursor import", command_path="ai-wiki admin",
        epilog=cli._examples(
            "ai-wiki admin changesets --principal process:ai-wiki-maintainer --since 2026-10-16T00:00:00Z",
            "ai-wiki admin revert --principal process:ai-wiki-maintainer --since 2026-10-16T00:00:00Z",
            "ai-wiki admin compare --live solvely-wiki --shadow solvely-wiki-shadow --since <R0>",
            "ai-wiki admin compare --live solvely-wiki --shadow solvely-wiki-shadow --since <R0> --blind key.json",
            "ai-wiki admin cursor import report.json",
        ), **common)
    verbs = admin.add_subparsers(dest="action", required=True)
    listing = verbs.add_parser(
        "changesets", help="list committed changesets, newest first", command_path="ai-wiki admin changesets",
        epilog=cli._examples("ai-wiki admin changesets --principal process:ai-wiki-maintainer --since 2026-10-16"),
        **common)
    listing.add_argument("--principal", help="only this principal's changesets")
    listing.add_argument("--since", help="only changesets created at this ISO date or time or later")
    listing.add_argument("--limit", type=cli._positive, default=100, help="maximum rows (default: 100)")
    listing.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    revert_ = verbs.add_parser(
        "revert", help="revert changesets on the writer, newest first; a conflict stops it",
        command_path="ai-wiki admin revert",
        epilog=cli._examples("ai-wiki admin revert --changeset 5e0c9a1b2f3d --reason 'wrong metric'",
                             "ai-wiki admin revert --principal process:ai-wiki-maintainer --since 2026-10-16 "
                             "--run INC-3"), **common)
    target = revert_.add_mutually_exclusive_group(required=True)
    target.add_argument("--changeset", metavar="ID", help="one changeset job id")
    target.add_argument("--principal", help="every changeset of this principal since --since")
    revert_.add_argument("--since", help="with --principal: ISO date or time")
    revert_.add_argument("--reason", help="why, for the revert commit's summary line")
    revert_.add_argument("--run", help="incident id for the commit's Run: trailer")
    revert_.add_argument("--wait", type=cli._positive, default=1800, help="seconds to wait for the receipt")
    revert_.add_argument("--json", action="store_true", help="emit the receipt as JSON")
    compare_ = verbs.add_parser(
        "compare", help="side-by-side diff of what live and shadow changed since a base revision",
        command_path="ai-wiki admin compare",
        epilog=cli._examples("ai-wiki admin compare --live solvely-wiki --shadow solvely-wiki-shadow --since a358395",
                             "ai-wiki admin compare --live solvely-wiki --shadow solvely-wiki-shadow --since a358395 "
                             "--blind key.json"), **common)
    compare_.add_argument("--live", required=True, metavar="BUNDLE")
    compare_.add_argument("--shadow", required=True, metavar="BUNDLE")
    compare_.add_argument("--since", required=True, metavar="R0",
                          help="the live bundle's commit the shadow started from")
    compare_.add_argument("--width", type=cli._positive, default=160, help="total columns (default: 160)")
    compare_.add_argument("--blind", type=Path, metavar="KEY_JSON",
                          help="for blind review: sides shuffled per concept as A and B, stamps left out; "
                               "the key (which side is which) goes to KEY_JSON")
    compare_.add_argument("--seed", type=int, help="with --blind: the shuffle seed (default: random)")
    cursor = verbs.add_parser("cursor", help="collector cursors", command_path="ai-wiki admin cursor",
                              epilog=cli._examples("ai-wiki admin cursor import report.json"), **common)
    cursor_verbs = cursor.add_subparsers(dest="cursor_action", required=True)
    importing = cursor_verbs.add_parser(
        "import", help="restore cursors from a maintenance report after writer disk loss",
        command_path="ai-wiki admin cursor import",
        epilog=cli._examples("ai-wiki admin cursor import report.json",
                             "ai-wiki admin cursor import report.json --replace"), **common)
    importing.add_argument("report", type=Path, help="report JSON with a cursors object (or the cursors object)")
    importing.add_argument("--replace", action="store_true", help="also overwrite cursors the writer already has")
    importing.add_argument("--run", default="admin:cursor-import", help="run recorded on each cursor")
    importing.add_argument("--json", action="store_true", help="emit JSON instead of TOON")


def command(a: argparse.Namespace, bundle: str | None) -> int:
    if a.action == "changesets":
        return changesets(bundle, principal=a.principal, since=a.since, limit=a.limit, as_json=a.json)
    if a.action == "revert":
        if bool(a.principal) != bool(a.since):
            cli._fail("--principal needs --since, and --since goes with --principal",
                      help_command="ai-wiki admin revert --help", code=2)
        return revert(bundle, changeset=a.changeset, principal=a.principal, since=a.since, reason=a.reason,
                      run=a.run, wait=a.wait, as_json=a.json)
    if a.action == "compare":
        if a.seed is not None and a.blind is None:
            cli._fail("--seed goes with --blind", help_command="ai-wiki admin compare --help", code=2)
        return compare(live=a.live, shadow=a.shadow, since=a.since, width=a.width, blind=a.blind, seed=a.seed)
    return cursor_import(bundle, a.report, replace=a.replace, run=a.run, as_json=a.json)


# --- HTTP ------------------------------------------------------------------------------------


def request(method: str, route: str, *, bundle: str | None = None, params: dict | None = None,
            body: dict | None = None, headers: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    """One call to the writer: status, lower-cased headers and body, error statuses included."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    try:
        # The writer answers a revert within ~60s before it falls back to 202.
        status, received, raw = cli._http(method, route, bundle=bundle, params=params, data=data, headers=headers,
                                          timeout=120)
    except OSError:  # unreachable, timed out, or the answer was cut short
        cli._fail("cannot reach the configured ai-wiki server", help_command="ai-wiki config show")
    return status, {key.lower(): value for key, value in received.items()}, raw


def _parsed(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _refused(status: int, payload: object) -> None:
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("message")
    message = f"server rejected the request (status {status})"
    if isinstance(detail, str) and detail.strip():
        message += f": {detail.strip()}"
    cli._fail(message, help_command="ai-wiki config show" if status == 401 else None)


def _json(status: int, raw: bytes) -> dict:
    """A 200 answer's JSON object, else the CLI's error."""
    payload = _parsed(raw)
    if status != 200 or not isinstance(payload, dict):
        _refused(status, payload)
    return payload


def _receipt(status: int, raw: bytes) -> dict:
    """A job receipt, whatever its HTTP status (409 and 422 are receipts too), else the error."""
    payload = _parsed(raw)
    if not isinstance(payload, dict) or "status" not in payload or "detail" in payload:
        _refused(status, payload)
    return payload


# --- changesets and revert ----------------------------------------------------------------------


def changesets(bundle: str | None, *, principal: str | None, since: str | None, limit: int, as_json: bool) -> int:
    status, _headers, raw = request("GET", "/admin/changesets", bundle=bundle,
                                    params={"principal": principal, "since": since, "limit": limit})
    listing = _json(status, raw)
    if as_json:
        print(json.dumps(listing, ensure_ascii=False, indent=2))
        return 0
    rows = [{"id": row.get("id"), "status": row.get("status"), "principal": row.get("principal"),
             "run": row.get("run"), "created": row.get("created"), "commit": (row.get("commit") or "")[:12] or None,
             "concepts": len(row.get("concept_files") or []) + len(row.get("deprecated_files") or []),
             "reverted_by": row.get("reverted_by")} for row in listing.get("changesets") or []]
    groups = [cli._count_lines(len(rows), listing.get("total")),
              table_lines("changesets", rows, ("id", "status", "principal", "run", "created", "commit", "concepts",
                                               "reverted_by"))]
    if rows:
        groups.append(table_lines("help", ({"command": "ai-wiki admin revert --changeset <id>",
                                            "purpose": "revert one changeset"},), ("command", "purpose")))
    emit(*groups)
    return 0


def _exit(job: dict) -> int:
    status = job.get("status")
    if status in ("done", "noop"):
        return CONFLICT if job.get("stopped") else 0
    if status == "rejected":
        return CONFLICT if job.get("http_status") == 409 else REJECTED
    return 1


def revert(bundle: str | None, *, changeset: str | None, principal: str | None, since: str | None,
           reason: str | None, run: str | None, wait: int, as_json: bool) -> int:
    """POST /admin/revert, then poll its job until the receipt is final or ``wait`` runs out."""
    body = {key: value for key, value in (("changeset", changeset), ("principal", principal), ("since", since),
                                          ("reason", reason)) if value is not None}
    status, _headers, raw = request("POST", "/admin/revert", bundle=bundle, body=body,
                                    headers={"X-AIWiki-Run": run} if run else None)
    job = _receipt(status, raw)
    deadline = time.monotonic() + wait
    while job.get("status") in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(POLL_S)
        status, _headers, raw = request("GET", f"/jobs/{urllib.parse.quote(job['id'], safe='')}", bundle=bundle)
        job = _json(status, raw)
    if as_json:
        print(json.dumps(job, ensure_ascii=False, indent=2))
        return _exit(job)
    stopped = job.get("stopped") or {}
    groups = [object_lines("revert", {
        "id": job.get("id"), "status": job.get("status"), "reverted": job.get("reverted") or [],
        "stopped_at": stopped.get("changeset"), "pending": job.get("pending") or [], "commit": job.get("commit"),
        **({"reverted_by": job["reverted_by"]} if job.get("reverted_by") else {})})]
    if stopped.get("conflicts"):
        groups.append(table_lines("conflicts", stopped["conflicts"], ("path", "changed_by")))
    if stopped.get("errors"):  # the problem undoing the stopped changeset would leave
        groups.append(table_lines("problems", stopped["errors"], ("code", "path", "message")))
    if job.get("errors"):
        groups.append(table_lines("errors", job["errors"], ("code", "path", "changed_by", "message")))
    elif isinstance(job.get("failure"), dict):
        groups.append(object_lines("failure", {key: job["failure"].get(key) for key in ("class", "stage", "detail")}))
    if job.get("status") in ("queued", "running"):
        groups.append(table_lines("help", ({"command": f"ai-wiki jobs {job.get('id')}",
                                            "purpose": "the revert is still queued; check it later"},),
                                  ("command", "purpose")))
    emit(*groups)
    return _exit(job)


# --- compare -----------------------------------------------------------------------------------


def _workspace(bundle: str, revision: str | None = None) -> tuple[str, dict[str, bytes]]:
    """The published tree of ``bundle`` (at ``revision``, an earlier published commit) and its revision."""
    status, headers, raw = request("GET", "/workspace", bundle=bundle, params={"revision": revision})
    if status != 200:
        _refused(status, _parsed(raw))
    tree = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            if member.isfile():
                tree[member.name] = archive.extractfile(member).read()
    return headers.get("x-aiwiki-revision", ""), tree


def compare(*, live: str, shadow: str, since: str, width: int, blind: Path | None = None,
            seed: int | None = None) -> int:
    base_revision, base = _workspace(live, since)
    live_revision, live_tree = _workspace(live)
    shadow_revision, shadow_tree = _workspace(shadow)
    if blind is not None:
        seed = secrets.randbelow(1 << 32) if seed is None else seed
        key = {"seed": seed, "since": base_revision, "live": {"bundle": live, "revision": live_revision},
               "shadow": {"bundle": shadow, "revision": shadow_revision},
               "A": blind_key(base, live_tree, shadow_tree, seed=seed)}
        try:
            blind.write_text(json.dumps(key, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            cli._fail(f"cannot write the blind key to {blind}: {exc}", code=2)
    print(render(base, live_tree, shadow_tree, names=(live, shadow),
                 revisions=(base_revision, live_revision, shadow_revision), width=width,
                 seed=None if blind is None else seed), end="")
    return 0


def _content(data: bytes | None) -> str | None:
    """Content identity: service-stamped keys and trailing whitespace never make two versions differ."""
    from aiwiki.runtime.changeset import content_hash

    return None if data is None else content_hash(data.decode("utf-8", errors="replace"))


def _state(before: bytes | None, after: bytes | None) -> str:
    if before is None:
        return "absent" if after is None else "created"
    if after is None:
        return "deleted"
    return "unchanged" if _content(before) == _content(after) else "changed"


def _cells(char: str) -> int:
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def _wrap(line: str | None, column: int) -> list[str]:
    """``line`` cut into pieces at most ``column`` terminal cells wide; CJK takes two cells."""
    pieces, piece, used = [], "", 0
    for char in (line or "").expandtabs(4):
        if used + _cells(char) > column:
            pieces.append(piece)
            piece, used = "", 0
        piece, used = piece + char, used + _cells(char)
    return [*pieces, piece]


def _pad(text: str, column: int) -> str:
    return text + " " * (column - sum(map(_cells, text)))


def _side_by_side(left: list[str], right: list[str], column: int, context: int = 3) -> list[str]:
    """``sdiff``-style rows: `|` changed, `<` only left, `>` only right; unchanged runs cut to context."""
    rows: list[str] = []

    def pair(a: str | None, b: str | None, mark: str) -> None:
        for n, (x, y) in enumerate(zip_longest(_wrap(a, column), _wrap(b, column), fillvalue="")):
            rows.append(f"{_pad(x, column)} {mark if n == 0 else ' '} {y}".rstrip())

    for group in difflib.SequenceMatcher(None, left, right, autojunk=False).get_grouped_opcodes(context):
        rows.append(f"@@ {group[0][1] + 1} | {group[0][3] + 1} @@")
        for tag, i1, i2, j1, j2 in group:
            for a, b in zip_longest(left[i1:i2], right[j1:j2]):
                pair(a, b, " " if tag == "equal" else "<" if b is None else ">" if a is None else "|")
    return rows


def _lines(data: bytes | None) -> list[str]:
    return [] if data is None else data.decode("utf-8", errors="replace").splitlines()


def _blinded(data: bytes | None) -> bytes | None:
    """``data`` without what names the flow: the service stamps (generated, verified, status),
    and each source's id and snapshot name, which differ by pipeline (``<sha>-<sha>`` for a
    Codex ingest, ``<evidence id>-<sha>`` for a changeset packet). The n-th source becomes S<n>
    in its id, its resource and its footnote labels, so both sides still read alike."""
    import yaml

    from aiwiki.engine import bookkeeping
    from aiwiki.runtime.changeset import SERVICE_KEYS

    try:
        frontmatter, body = bookkeeping._split(data.decode("utf-8", errors="replace"))
    except (AttributeError, bookkeeping.BookkeepingError):  # absent, or no frontmatter to strip
        return data
    kept = [block for block in bookkeeping._blocks(frontmatter) if block[0] not in SERVICE_KEYS]
    try:
        sources = (yaml.safe_load("".join(bookkeeping._lines(kept, "sources") or [])) or {}).get("sources")
    except (yaml.YAMLError, AttributeError):
        sources = None
    for index, source in enumerate(sources if isinstance(sources, list) else [], 1):
        label = f"S{index}"
        for key in ("id", "resource"):
            value = source.get(key) if isinstance(source, dict) else None
            if not isinstance(value, str) or not value:
                continue
            placeholder = label if key == "id" else f"/sources/{label}"
            field = re.compile(rf"(\b{key}:\s*['\"]?){re.escape(value)}(?=['\"]?\s*(?:[,}}]|$))", re.M)
            kept = [(name, [field.sub(rf"\g<1>{placeholder}", line) for line in lines])
                    if name == "sources" else (name, lines) for name, lines in kept]
            if key == "id":
                body = body.replace(f"[^{value}]", f"[^{label}]")
    return f"---\n{bookkeeping._join(kept)}---\n{body}".encode()


def _changes(base: dict[str, bytes], live: dict[str, bytes], shadow: dict[str, bytes]):
    """The concepts each bundle changed since ``base``; those whose contents differ; those alike."""
    from aiwiki.engine.validate import should_check

    paths = sorted(rel for rel in set(base) | set(live) | set(shadow) if should_check(Path(rel), Path(".")))
    changed = {name: [rel for rel in paths if _state(base.get(rel), tree.get(rel)) not in ("unchanged", "absent")]
               for name, tree in (("live", live), ("shadow", shadow))}
    touched = sorted(set(changed["live"]) | set(changed["shadow"]))
    differing = [rel for rel in touched if _content(live.get(rel)) != _content(shadow.get(rel))]
    return changed, differing, [rel for rel in touched if rel not in differing]


def _a_side(seed: int, rel: str) -> str:
    """The bundle blind column A shows for ``rel``: a coin flip fixed by the seed and the path."""
    return ("live", "shadow")[hashlib.sha256(f"{seed}:{rel}".encode()).digest()[0] & 1]


def blind_key(base: dict[str, bytes], live: dict[str, bytes], shadow: dict[str, bytes], *, seed: int) -> dict:
    """``{concept: bundle}``: which bundle, live or shadow, is side A of each blind section."""
    return {rel: _a_side(seed, rel) for rel in _changes(base, live, shadow)[1]}


def render(base: dict[str, bytes], live: dict[str, bytes], shadow: dict[str, bytes], *,
           names: tuple[str, str], revisions: tuple[str, str, str], width: int = 160,
           seed: int | None = None) -> str:
    """The concepts either bundle changed since ``base``, side by side where their contents differ.

    Deterministic for the same trees: paths in order, one section per differing concept, and
    concepts both changed to the same content listed once as identical. With ``seed`` it is
    blind: no bundle is named, each section shows the two sides as A and B in the order
    ``blind_key`` records, and the service stamps are left out.
    """
    changed, differing, identical = _changes(base, live, shadow)
    if seed is None:
        lines = [f"compare live={names[0]}@{revisions[1][:12]} shadow={names[1]}@{revisions[2][:12]} "
                 f"since={revisions[0][:12]}",
                 f"concepts changed: live {len(changed['live'])}, shadow {len(changed['shadow'])}; "
                 f"differing {len(differing)}, identical {len(identical)}"]
    else:
        lines = [f"compare blind since={revisions[0][:12]}",
                 f"concepts changed: {len(differing) + len(identical)}; "
                 f"differing {len(differing)}, identical {len(identical)}"]
    if identical:
        lines.append("identical: " + ", ".join(identical))
    column = (width - 3) // 2
    for rel in differing:
        sides = [("live", names[0], live.get(rel)), ("shadow", names[1], shadow.get(rel))]
        if seed is not None:
            ordered = sides[::-1] if _a_side(seed, rel) == "shadow" else sides
            sides = [(label, label, _blinded(data)) for label, (_, _, data) in zip(("A", "B"), ordered, strict=True)]
        (left_label, left_head, left), (right_label, right_head, right) = sides
        lines += ["", f"== {rel} ({left_label}: {_state(base.get(rel), left)}, "
                      f"{right_label}: {_state(base.get(rel), right)})",
                  f"{_pad(left_head, column)}   {right_head}".rstrip()]
        lines += _side_by_side(_lines(left), _lines(right), column)
    return "\n".join(lines) + "\n"


# --- cursor import -------------------------------------------------------------------------------


def _cursors(report: Path) -> dict[str, dict]:
    """``{name: value}`` from a report's ``cursors`` object of GET /maint/cursors records."""
    from aiwiki.service.maint_state import CURSORS

    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        cli._fail(f"cannot read a JSON report from {report}: {exc}", code=2)
    cursors = data.get("cursors", data) if isinstance(data, dict) else None
    values = {}
    for name, record in cursors.items() if isinstance(cursors, dict) else ():
        value = record.get("value") if isinstance(record, dict) else None
        if name not in CURSORS or not isinstance(value, dict):
            cli._fail(f"cursor {name!r} must be one of {', '.join(CURSORS)} with an object value", code=2)
        values[name] = value
    if not values:
        cli._fail(f"{report} holds no cursors", code=2)
    return values


def cursor_import(bundle: str | None, report: Path, *, replace: bool, run: str, as_json: bool) -> int:
    """Write each cursor of the report by compare-and-swap. One the writer already holds with
    another value is kept unless ``replace``: a restored nightly copy may be the newer one."""
    rows = put_cursors(bundle, _cursors(report), replace=replace, run=run)
    return show_cursors(rows, as_json=as_json, replace_command=f"ai-wiki admin cursor import {report} --replace")


def put_cursors(bundle: str | None, values: dict[str, dict], *, replace: bool, run: str) -> list[dict]:
    """Create or compare-and-swap each ``{name: value}`` cursor; ``{cursor, outcome, etag}`` rows.
    A cursor the writer holds with another value is kept unless ``replace``."""
    rows = []
    for name, value in sorted(values.items()):
        route = f"/maint/cursors/{name}"
        status, _headers, raw = request("GET", route, bundle=bundle)
        if status == 404:
            condition, outcome = {"If-None-Match": "*"}, "created"
        else:
            current = _json(status, raw)
            if current.get("value") == value or not replace:
                rows.append({"cursor": name, "outcome": "unchanged" if current.get("value") == value else "kept",
                             "etag": current.get("etag")})
                continue
            condition, outcome = {"If-Match": f'"{current.get("etag")}"'}, "replaced"
        status, _headers, raw = request("PUT", route, bundle=bundle, body={"value": value},
                                        headers={**condition, "X-AIWiki-Run": run})
        rows.append({"cursor": name, "outcome": outcome, "etag": _json(status, raw).get("etag")})
    return rows


def show_cursors(rows: list[dict], *, as_json: bool, replace_command: str) -> int:
    """Print ``put_cursors`` rows; exit 1 when a cursor was kept rather than overwritten."""
    kept = [row["cursor"] for row in rows if row["outcome"] == "kept"]
    if as_json:
        print(json.dumps({"cursors": rows}, ensure_ascii=False, indent=2))
    else:
        groups = [table_lines("cursors", rows, ("cursor", "outcome", "etag"))]
        if kept:
            groups.append(table_lines("help", ({"command": replace_command,
                                                "purpose": "overwrite " + ", ".join(kept)},), ("command", "purpose")))
        emit(*groups)
    return 1 if kept else 0
