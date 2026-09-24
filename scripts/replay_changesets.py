#!/usr/bin/env python3
"""Historical replay of curate changesets (design §10.3), the phase-1 exit gate.

Each of the newest ``--count`` production ingests (commits titled ``ingest: ...``) is
proposed again as one curate changeset: the concept bytes that ingest committed and its
source snapshot, on the ingest's parent commit. The changeset runs through the writer
transaction (``runtime.curate.run_changeset``) in a scratch clone whose origin is a scratch
bare copy of ``--repo``; ``--repo`` itself is only cloned. No LLM runs.

The replay passes when every changeset commits and its tree differs from the historical
commit only where the service owns the bytes: ``generated``, ``verified``, ``status``,
``sources[].resource`` and spilled bookkeeping in concepts, plus ``log.md`` and ``viz.html``.
Those service-owned values must also be the ones design §2.4 prescribes, and indexes and
``sources/.hashes.yaml`` must match exactly. The evidence id is the historical snapshot's
name stem, so the packet lands at the path the concepts already cite.

The bytes are proposed verbatim, so a retitle or deep cut the ingest made is refused by
the gate. ``--declare-allow`` declares it in ``allow``, as a compliant agent must; the
report counts the ingests that needed it (``allow_declared``) apart from those accepted
verbatim (``accepted_verbatim``).

Prints one JSON report. Exit 0 = every ingest accepted and equivalent, 1 = not, 2 = usage.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from aiwiki.engine import bookkeeping
from aiwiki.engine.document import OKFDocumentError, parse_document
from aiwiki.engine.update_concept import SHRINK_RATIO
from aiwiki.engine.validate import split_body_spill
from aiwiki.runtime import changeset, curate

DEFAULT_ACTOR = "process:ai-wiki-maintainer"
# Concept differences the service owns (design §2.4; spill is bookkeeping the gate strips
# from the body). Anything else breaks equivalence.
SERVICE_DIFFS = frozenset({"generated", "verified", "status", "sources[].resource", "spill"})
# Closeout that differs by design: the log line's date and subject, and the status and
# generated stamps viz.html embeds. Indexes and the hash ledger follow from the tree.
CLOSEOUT = frozenset({"log.md", "viz.html"})
_SNAPSHOT = re.compile(r"(?P<stem>.+)-(?P<sha>[0-9a-f]{64})(?P<ext>\..+)")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          encoding="utf-8").stdout


def _diff(repo: Path, *args: str) -> list[str]:
    """``git diff`` fields, NUL-separated so non-ASCII paths arrive unquoted."""
    return _git(repo, "diff", "-z", "--no-renames", *args).split("\0")[:-1]


def _show(repo: Path, revision: str, rel: str) -> bytes | None:
    shown = subprocess.run(["git", "-C", str(repo), "show", f"{revision}:{rel}"], capture_output=True)
    return shown.stdout if shown.returncode == 0 else None


def ingests(repo: Path, ref: str, count: int) -> list[tuple[str, str]]:
    """``(commit, parent)`` of the newest ``count`` ingest commits on ``ref``, newest first."""
    found = []
    for line in _git(repo, "log", "--first-parent", "--format=%H %P%x1f%s", ref).splitlines():
        revisions, subject = line.split("\x1f", 1)
        if subject.startswith("ingest: "):
            commit, *parents = revisions.split()
            if len(parents) != 1:
                raise ValueError(f"ingest {commit[:12]} is a merge; replay needs one parent")
            found.append((commit, parents[0]))
            if len(found) == count:
                break
    return found


def _is_concept(rel: str) -> bool:
    return changeset._path_error(rel) is None


def _identity_and_body(text: str) -> tuple[tuple[object, object], str]:
    document = parse_document(text)
    return (document.frontmatter.get("type"), document.frontmatter.get("title")), document.body.strip()


def build_request(repo: Path, commit: str, parent: str) -> dict:
    """The changeset an agent would propose for this ingest: its committed concept bytes and
    its source snapshot, uploaded under the evidence id that stores it at the same path.

    A retitle or a deep cut the ingest made is listed in ``allow``, the declaration the
    gate requires of any agent making that edit on purpose.
    """
    fields = _diff(repo, "--name-status", parent, commit)
    changed = list(zip(fields[::2], fields[1::2], strict=True))
    removed = [rel for status, rel in changed if status == "D" and _is_concept(rel)]
    if removed:
        raise ValueError(f"the ingest removed concepts, which a changeset cannot: {', '.join(removed)}")
    snapshots = [rel for status, rel in changed
                 if status == "A" and rel.startswith("sources/") and rel != "sources/.hashes.yaml"]
    if len(snapshots) != 1:
        raise ValueError(f"expected one new source snapshot, found {snapshots}")
    data = _show(repo, commit, snapshots[0])
    match = _SNAPSHOT.fullmatch(snapshots[0].removeprefix("sources/"))
    if data is None or not match or match["sha"] != hashlib.sha256(data).hexdigest():
        raise ValueError(f"{snapshots[0]} is not a content-addressed source snapshot")
    # write_source stores an upload as <slug(stem)>-<sha><ext>, Markdown as .md.source.
    suffix = {".md.source": ".md", ".source": ""}.get(match["ext"], match["ext"])
    files, allow = [], {"retype": [], "shrink": []}
    for _status, rel in changed:
        if not _is_concept(rel):
            continue
        old = _show(repo, parent, rel)
        before, after = None if old is None else old.decode("utf-8"), _show(repo, commit, rel).decode("utf-8")
        files.append({"path": rel, "op": "put", "content": after,
                      "base": None if before is None else changeset.content_hash(before)})
        if before is None:
            continue
        (identity, body), (new_identity, new_body) = _identity_and_body(before), _identity_and_body(after)
        if new_identity != identity:
            allow["retype"].append({"path": rel, "reason": f"ingest {commit[:12]} changed its type or title"})
        if body and len(new_body) < SHRINK_RATIO * len(body):
            allow["shrink"].append({"path": rel, "reason": f"ingest {commit[:12]} shortened its body"})
    request = {
        "schema": changeset.SCHEMA, "kind": "curate", "intent": "evidence", "base_revision": parent,
        "work_items": [],
        "evidence": {"id": match["stem"], "upload": {"filename": match["stem"] + suffix,
                                                     "content_b64": base64.b64encode(data).decode("ascii")}},
        "files": files,
        "message": f"replay: ingest {commit[:12]}",
    }
    if any(allow.values()):
        request["allow"] = {name: entries for name, entries in allow.items() if entries}
    return request


def _without_resource(sources: object) -> object:
    if not isinstance(sources, list):
        return sources
    return [{key: value for key, value in source.items() if key != "resource"} if isinstance(source, dict)
            else source for source in sources]


def _residue(text: str, keys: set[str]) -> str:
    """The document without the frontmatter blocks of ``keys`` and without body spill:
    what must stay byte-equal."""
    frontmatter, body = bookkeeping._split(text)
    kept = [block for block in bookkeeping._blocks(frontmatter) if block[0] not in keys]
    return bookkeeping._join(kept) + split_body_spill(body)[1]


def concept_diff(historical: str | None, replayed: str | None) -> list[str]:
    """Where a replayed concept differs from the historical one.

    Names each differing frontmatter key (``sources[].resource`` when sources differ only
    there); ``spill`` when the bodies differ only in leading spilled frontmatter or
    verification lines, which the service removes, else ``body``; ``missing``/``unparsed``
    when a side has no document; ``format`` when the parsed fields agree but bytes outside
    the service-owned keys do not.
    """
    if historical is None or replayed is None:
        return ["missing"]
    try:
        old, new = parse_document(historical), parse_document(replayed)
    except OKFDocumentError:
        return ["unparsed"]
    diff = []
    for key in sorted(set(old.frontmatter) | set(new.frontmatter), key=str):
        before, after = old.frontmatter.get(key), new.frontmatter.get(key)
        if before != after:
            same = key == "sources" and _without_resource(before) == _without_resource(after)
            diff.append("sources[].resource" if same else str(key))
    if old.body != new.body:
        same = split_body_spill(old.body)[1] == split_body_spill(new.body)[1]
        diff.append("spill" if same else "body")
    if set(diff) <= SERVICE_DIFFS:
        owned = {"generated", "verified", "status"} | ({"sources"} if "sources[].resource" in diff else set())
        if _residue(historical, owned) != _residue(replayed, owned):
            diff.append("format")
    return diff


def service_values(parent: str | None, replayed: str, actor: str) -> list[str]:
    """Where a replayed concept's service-owned values break design §2.4 for a curate
    changeset: ``status`` kept from the parent (``draft`` when new), ``verified`` restored
    from it exactly, ``generated`` restored from it or stamped by ``actor``."""
    before = {} if parent is None else parse_document(parent).frontmatter
    after = parse_document(replayed).frontmatter
    wrong = []
    if after.get("status") != ("draft" if parent is None else before.get("status")):
        wrong.append("status_value")
    if after.get("verified") != before.get("verified"):
        wrong.append("verified_value")
    generated = after.get("generated")
    if generated != before.get("generated") and not (isinstance(generated, dict) and generated.get("by") == actor):
        wrong.append("generated_value")
    return wrong


def _text(repo: Path, revision: str, rel: str) -> str | None:
    data = _show(repo, revision, rel)
    return None if data is None else data.decode("utf-8")


def compare(repo: Path, historical: str, replayed: str, *, parent: str, actor: str) -> dict:
    """Classify every path where the replayed commit's tree differs from the historical one.

    A differing concept also names each service-owned value that breaks §2.4 against
    ``parent`` (``status_value``, ``verified_value``, ``generated_value``).
    """
    concepts, closeout, other = {}, [], []
    for rel in _diff(repo, "--name-only", historical, replayed):
        if _is_concept(rel):
            old, new = _text(repo, historical, rel), _text(repo, replayed, rel)
            concepts[rel] = concept_diff(old, new)
            if new is not None and "unparsed" not in concepts[rel]:
                concepts[rel] += service_values(_text(repo, parent, rel), new, actor)
        elif rel in CLOSEOUT:
            closeout.append(rel)
        else:
            other.append(rel)
    return {"concepts": concepts, "closeout": closeout, "other": other}


def replay(repo: Path, remote: Path, work: Path, commit: str, parent: str, *, branch: str, actor: str,
           declare_allow: bool = False) -> dict:
    """Propose one historical ingest as a changeset on its parent and judge the result.

    The ``allow`` its edits need is reported, but sent only when ``declare_allow``.
    """
    result: dict = {"commit": commit, "parent": parent, "allow_declared": False}
    try:
        request = build_request(repo, commit, parent)
    except ValueError as exc:
        return {**result, "accepted": False, "equivalent": False, "error": str(exc)}
    result.update(evidence_id=request["evidence"]["id"], files=len(request["files"]))
    if request.get("allow"):
        result["allow"] = {name: [entry["path"] for entry in entries] for name, entries in request["allow"].items()}
        if declare_allow:
            result["allow_declared"] = True
        else:
            del request["allow"]
    _git(remote, "update-ref", f"refs/heads/{branch}", parent)
    writer = work / commit[:12]
    subprocess.run(["git", "clone", "-q", "-b", branch, str(remote), str(writer)], check=True, capture_output=True)
    _git(writer, "config", "user.email", "replay@ai-wiki.invalid")
    _git(writer, "config", "user.name", "ai-wiki replay")
    job_path = writer / ".okf" / "jobs" / f"{commit[:12]}.json"
    curate.run_changeset(writer, job_path, request, actor=actor)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    pushed = (job.get("git") or {}).get("pushed")
    result.update(status=job.get("status"), replay_commit=job.get("commit"), source=job.get("source_snapshot"))
    result["accepted"] = job.get("status") == "done" and bool(pushed)
    if not result["accepted"]:
        result.update(error=job.get("error"), http_status=job.get("http_status"), errors=job.get("errors"),
                      validation=job.get("validation"), equivalent=False)
        return result
    result["warnings"] = sorted({warning["code"] for warning in job.get("warnings") or ()})
    result["diff"] = compare(remote, commit, job["commit"], parent=parent, actor=actor)
    result["equivalent"] = not result["diff"]["other"] and all(
        set(keys) <= SERVICE_DIFFS for keys in result["diff"]["concepts"].values()
    )
    shutil.rmtree(writer, ignore_errors=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo", required=True, type=Path, help="bundle Git repository (read-only; cloned)")
    parser.add_argument("--ref", default="main", help="branch holding the production history (default main)")
    parser.add_argument("--count", type=int, default=10, help="newest ingests to replay (default 10)")
    parser.add_argument("--actor", default=DEFAULT_ACTOR, help=f"changeset actor (default {DEFAULT_ACTOR})")
    parser.add_argument("--work", type=Path, help="scratch directory (default: a new temporary one, removed)")
    parser.add_argument("--declare-allow", action="store_true",
                        help="declare the retitles and deep cuts an ingest made in allow (default: verbatim)")
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("--count must be at least 1")
    os.environ.pop("AIWIKI_GIT", None)  # the writer transaction must commit and push
    repo = args.repo.expanduser().resolve()
    history = ingests(repo, args.ref, args.count)
    # Resolved: the writer compares its paths with Git's (macOS /var is a symlink).
    work = Path(tempfile.mkdtemp(prefix="aiwiki-replay-", dir=args.work)).resolve()
    try:
        remote = work / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True, capture_output=True)
        _git(remote, "remote", "remove", "origin")  # nothing here may reach the production remote
        with contextlib.redirect_stdout(sys.stderr):  # closeout reports on stdout; the JSON owns it
            results = [replay(repo, remote, work, commit, parent, branch=args.ref, actor=args.actor,
                              declare_allow=args.declare_allow) for commit, parent in history]
    finally:
        if args.work is None:
            shutil.rmtree(work, ignore_errors=True)
    summary = {
        "declare_allow": args.declare_allow,
        "replayed": len(results),
        "accepted": sum(result["accepted"] for result in results),
        "accepted_verbatim": sum(result["accepted"] and not result["allow_declared"] for result in results),
        "allow_declared": sum(result["allow_declared"] for result in results),
        "equivalent": sum(result["equivalent"] for result in results),
    }
    summary["passed"] = len(results) == args.count and summary["equivalent"] == args.count
    print(json.dumps({**summary, "ingests": results}, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
