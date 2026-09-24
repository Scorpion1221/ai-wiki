"""Local ``ai-wiki validate`` and the writer's dry-run judge alike (design §2.9, §10.1).

Each case edits a pulled workspace; the CLI judges the changeset locally (``validate``)
and asks the writer (``propose --dry-run``) with the same options. Both must reach the
same status and HTTP status, the same ``(code, path)`` errors and warnings, and the same
changeset digest. The ~60 synthetic mutations (YAML, provenance, links, protection,
out-of-bounds paths, secrets, limits) run on the fixture bundle. With
``AIWIKI_PARITY_BUNDLE`` naming a clone of the live bundle, every one of its (300+) real
concepts is edited and judged in one changeset too; its content never enters the repository.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from gate_fixture import AI_STUDY, AIO_AB, EVIDENCE, EVIDENCE_ID, METRIC, Gate, cite, concept
from gate_fixture import cited as column_zero_citation
from test_cli_workspace import wiki, wiki_json

from aiwiki.cli import workspace
from aiwiki.engine.validate import should_check

VIEWS = "metrics/view-references-exposure-proxy-2026-09.md"
VISITORS = "metrics/web-landing-new-visitor-distribution-2026-09.md"
COUNTRY = "metrics/ai-study-country-registration-payment-2026-09.md"
LIVE_BUNDLE = os.environ.get("AIWIKI_PARITY_BUNDLE")


def edit(ws: Path, rel: str, change) -> None:
    path = ws / rel
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8")


def put(ws: Path, rel: str, text: str | bytes) -> None:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))


def frontmatter(text: str, line: str) -> str:
    """``text`` with ``line`` added as the last frontmatter line."""
    head, _sep, body = text[4:].partition("\n---\n")
    return f"---\n{head}\n{line}\n---\n{body}"


def shrunk(text: str) -> str:
    head, _sep, _body = text[4:].partition("\n---\n")
    return cite(f"---\n{head}\n---\n# Summary\n\nShort.\n")


def evidence(name: str, data: bytes):
    """Upload ``data`` as the packet instead of the default evidence file."""
    def args(ws: Path) -> list:
        put(ws.parent, name, data)
        return ["--upload", ws.parent / name, "--source-id", EVIDENCE_ID]
    return args


SECRET = "ghp_" + "a1b2" * 9
ITEM = "<the claimed work item>"
# (name, expected status or one expected error code, mutation of the workspace -> extra CLI args or None)
CASES = [
    # accepted, or nothing to do
    ("cited edit", "would_apply", lambda ws: edit(ws, METRIC, cite)),
    ("new concept", "would_apply", lambda ws: put(ws, "metrics/probe.md", concept("Probe"))),
    ("two cited edits", "would_apply", lambda ws: [edit(ws, rel, cite) for rel in (METRIC, VIEWS)] and None),
    ("service keys only", "noop", lambda ws: edit(ws, METRIC, lambda t: t.replace("status: stable", "status: draft"))),
    ("writes verified", "would_apply", lambda ws: edit(ws, COUNTRY, lambda t: cite(t).replace(
        "verified:\n", "verified:\n  - {by: process:ai-wiki-maintainer, at: 2026-09-24T00:00:00Z}\n"))),
    ("writes generated", "would_apply", lambda ws: edit(ws, METRIC, lambda t: cite(t).replace(
        "  by: process:ai-wiki-curator\n", "  by: human:someone\n"))),
    ("writes status on a new concept", "would_apply",
     lambda ws: put(ws, "metrics/probe.md", concept("Probe", "status: stable\n"))),
    ("drops a source", "would_apply", lambda ws: edit(ws, METRIC, lambda t: cite(re.sub(
        r"- id: dated-first-payment.*?(?=aliases:)", "", t, flags=re.S)))),
    ("deprecates", "would_apply", lambda ws: edit(ws, METRIC, cite) or [
        "--deprecate", f"{AI_STUDY}:{METRIC}:folded into the funnel metric"]),
    ("allowed shrink", "would_apply", lambda ws: edit(ws, AI_STUDY, shrunk) or [
        "--allow-shrink", f"{AI_STUDY}:readout moved to the funnel metric"]),
    ("binary packet", "would_apply", lambda ws: edit(ws, METRIC, cite) or evidence(
        "chart.png", b"\x89PNG\r\n\x00")(ws)),
    ("work item evidence", "would_apply", lambda ws: edit(ws, METRIC, cite) or ["--item", ITEM]),
    ("whitespace only", "noop", lambda ws: edit(ws, METRIC, lambda t: t.replace("# Summary\n", "# Summary  \n"))),
    # YAML and the profile
    ("list item at column zero", "yaml_parse", lambda ws: edit(ws, AI_STUDY, column_zero_citation)),
    ("unquoted colon", "yaml_parse", lambda ws: edit(ws, METRIC, lambda t: cite(t).replace(
        "title: Redacted title", "title: Redacted: title"))),
    ("tab indent", "yaml_parse", lambda ws: edit(ws, METRIC, lambda t: cite(t).replace(
        "\n  title: Redacted title\n", "\n\ttitle: Redacted title\n"))),
    ("no closing delimiter", "yaml_parse", lambda ws: edit(ws, METRIC, lambda t: cite(t).replace(
        "---\n# Summary", "# Summary"))),
    ("impossible date", "yaml_parse", lambda ws: edit(ws, METRIC, lambda t: frontmatter(
        cite(t), "stale_after: 2026-02-30"))),
    ("stale_after not a date", "invalid_value", lambda ws: edit(ws, METRIC, lambda t: frontmatter(
        cite(t), "stale_after: soon"))),
    ("missing description", "missing_key", lambda ws: put(ws, "metrics/probe.md", concept("Probe").replace(
        "description: A probe concept\n", ""))),
    ("empty tags", "missing_key", lambda ws: put(ws, "metrics/probe.md", concept("Probe").replace(
        "tags: [metric]", "tags: []"))),
    ("type not a string", "invalid_value", lambda ws: put(ws, "metrics/probe.md", concept("Probe").replace(
        "type: Metric", "type: 5"))),
    ("legacy key", "legacy_key", lambda ws: edit(ws, METRIC, lambda t: frontmatter(cite(t), "timestamp: 2026-09-01"))),
    ("legacy citations", "legacy_key", lambda ws: edit(ws, METRIC, lambda t: cite(t) + "\n# Citations\n\n- x\n")),
    ("frontmatter not a mapping", "yaml_parse", lambda ws: put(ws, "metrics/probe.md", "---\n- a\n---\n# X\n")),
    ("duplicate key (the last one wins)", "would_apply", lambda ws: put(ws, "metrics/probe.md", concept(
        "Probe", "title: Probe again\n"))),
    ("spilled frontmatter", "yaml_parse", lambda ws: edit(ws, METRIC, lambda t: cite(t).replace(
        "---\n# Summary", "---\nstale_after: 2026-12-31\n# Summary"))),
    ("an edit repairs a spilled audit event", "would_apply", lambda ws: edit(ws, AIO_AB, cite)),
    ("usage_window not a window", "invalid_value", lambda ws: put(ws, "metrics/probe.md", concept(
        "Probe", "usage_window: last week\n"))),
    # provenance
    ("uncited edit", "uncited_change", lambda ws: edit(ws, VIEWS, lambda t: t + "\nA new claim.\n")),
    ("new concept without sources", "missing_key", lambda ws: put(ws, "metrics/probe.md", re.sub(
        r"sources:\n- .*\n", "", concept("Probe")))),
    ("new concept citing an old source", "uncited_change", lambda ws: put(ws, "metrics/probe.md", concept(
        "Probe").replace("resource: evidence:packet", "resource: /sources/" + next(
            (ws / "sources").glob("evidence-*")).name))),
    ("wrong packet id", "unknown_evidence_ref", lambda ws: edit(ws, METRIC, lambda t: cite(t, "other-id"))),
    ("foreign evidence scheme", "unknown_evidence_ref", lambda ws: edit(ws, METRIC, lambda t: cite(t).replace(
        "- id: dated-first", "- id: x\n  resource: evidence:other\n- id: dated-first"))),
    ("unresolvable resource", "resource_unresolvable", lambda ws: edit(ws, METRIC, lambda t: cite(t).replace(
        "aliases:", "- {id: gone, resource: /sources/gone.md.source}\naliases:"))),
    ("evidence id already taken", "invalid_value", lambda ws: edit(ws, METRIC, lambda t: cite(
        t, "dated-first-payment-renewal-ltv-cpa-2026-09-17")) or [
        "--source-id", "dated-first-payment-renewal-ltv-cpa-2026-09-17"]),
    ("secret in the packet", "secret_detected", lambda ws: edit(ws, METRIC, cite) or evidence(
        "leak.md", f"token {SECRET}\n".encode())(ws)),
    ("secret in a concept", "secret_detected", lambda ws: edit(ws, METRIC, lambda t: cite(t) + f"\nKey {SECRET}.\n")),
    ("empty packet", "input", lambda ws: edit(ws, METRIC, cite) or evidence("empty.md", b" \n")(ws)),
    ("oversized packet", "too_large", lambda ws: edit(ws, METRIC, cite) or evidence(
        "big.md", b"x" * (1024 * 1024 + 1))(ws)),
    # links and the concept graph
    ("broken link", "broken_link", lambda ws: edit(ws, METRIC, lambda t: cite(t) + "\nSee [gone](nope.md).\n")),
    ("link out of the bundle", "broken_link", lambda ws: edit(ws, METRIC, lambda t: cite(t) + "\n[x](../../x.md)\n")),
    ("dangling contradiction", "dangling_contradiction", lambda ws: edit(ws, METRIC, lambda t: frontmatter(
        cite(t), "contradictions: [nope.md]"))),
    ("duplicate title", "duplicate_title", lambda ws: put(ws, "metrics/probe.md", concept("Redacted title"))),
    ("duplicate alias", "duplicate_title", lambda ws: put(ws, "metrics/probe.md", concept(
        "Probe", "aliases: [Redacted alias 1]\n"))),
    ("deprecate to a missing successor", "broken_link", lambda ws: edit(ws, METRIC, cite) or [
        "--deprecate", f"{AI_STUDY}:metrics/nope.md:gone"]),
    ("deprecate to itself", "broken_link", lambda ws: edit(ws, METRIC, cite) or [
        "--deprecate", f"{AI_STUDY}:{AI_STUDY}:self"]),
    ("deprecate a chain", "broken_link", lambda ws: edit(ws, METRIC, cite) or [
        "--deprecate", f"{AI_STUDY}:{VIEWS}:chain", "--deprecate", f"{VIEWS}:{METRIC}:chain"]),
    ("deprecate too many", "too_large", lambda ws: edit(ws, METRIC, cite) or [
        arg for rel in (AI_STUDY, VIEWS, VISITORS, COUNTRY) for arg in ("--deprecate", f"{rel}:{METRIC}:many")]),
    ("deprecate a missing concept", "input", lambda ws: edit(ws, METRIC, cite) or [
        "--deprecate", f"metrics/nope.md:{METRIC}:gone"]),
    # protection
    ("body shrink", "body_shrink", lambda ws: edit(ws, AI_STUDY, shrunk)),
    ("retitle", "identity_locked", lambda ws: edit(ws, METRIC, lambda t: cite(t).replace(
        "title: Redacted title", "title: Another title"))),
    ("retype", "identity_locked", lambda ws: edit(ws, METRIC, lambda t: cite(t).replace(
        "type: Metric", "type: Reference"))),
    ("allowed retitle", "would_apply", lambda ws: edit(ws, METRIC, lambda t: cite(t).replace(
        "title: Redacted title", "title: Another title")) or [
        "--allow-retype", f"{METRIC}:the experiment was renamed upstream"]),
    ("delete a concept", "delete_forbidden", lambda ws: (ws / VIEWS).unlink()),
    # out of bounds
    ("SCHEMA.md", "service_owned_path", lambda ws: put(ws, "SCHEMA.md", "# Schema\n")),
    ("purpose.md", "service_owned_path", lambda ws: put(ws, "purpose.md", "# Purpose\n")),
    ("index.md", "service_owned_path", lambda ws: edit(ws, "index.md", lambda t: t + "\n- extra\n")),
    ("log.md", "service_owned_path", lambda ws: put(ws, "log.md", "# Log\n")),
    ("viz.html", "service_owned_path", lambda ws: put(ws, "viz.html", "<html></html>\n")),
    ("a new source", "service_owned_path", lambda ws: put(ws, "sources/new.md.source", "evidence\n")),
    ("the source hashes", "service_owned_path", lambda ws: edit(ws, "sources/.hashes.yaml", lambda t: t + "# x\n")),
    ("hidden file", "path_forbidden", lambda ws: put(ws, "metrics/.draft.md", concept("Draft"))),
    ("not markdown", "path_forbidden", lambda ws: put(ws, "metrics/data.csv", "a,b\n")),
    ("too many files", "too_large", lambda ws: [put(ws, f"metrics/probe-{index}.md", concept(f"Probe {index}"))
                                               for index in range(21)] and None),
    ("concept too large", "too_large", lambda ws: put(ws, "metrics/probe.md", concept("Probe", body="x" * 140000))),
]


def reset(ws: Path) -> None:
    """Put the workspace back to its pulled base."""
    changed, _local = workspace.changes(ws, workspace.load(ws))
    for rel, change in changed.items():
        if change == "added":
            (ws / rel).unlink()
        else:
            put(ws, rel, (ws / ".ai-wiki" / "base" / rel).read_bytes())


def verdict(result: dict) -> tuple:
    return (result.get("status"), result.get("http_status"), result.get("changeset_sha256"),
            sorted({(error["code"], error.get("path")) for error in result.get("errors") or []}),
            sorted({(warning["code"], warning.get("path")) for warning in result.get("warnings") or []}))


def judged_alike(capsys, ws: Path, args: list) -> tuple[tuple, tuple]:
    local_code, local = wiki_json(capsys, "validate", "--dir", ws, *args)
    server_code, server = wiki_json(capsys, "propose", "--dir", ws, *args, "--dry-run", "--state-dir", ws.parent / "st")
    assert "id" not in server and server.get("status") in ("would_apply", "noop", "rejected"), server  # no job
    assert local_code == server_code, (local, server)
    return verdict(local), verdict(server)


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch)
    gate.connect()
    yield gate
    gate.close()


def test_local_validate_and_the_dry_run_agree_on_every_mutation(gate, tmp_path, capsys) -> None:
    ws = tmp_path / "ws"
    assert wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 0
    put(tmp_path, "status.md", EVIDENCE)
    item = gate.item(run="WAIO-1")
    mismatches, surprises = [], []
    assert len(CASES) >= 60

    for name, expected, mutate in CASES:
        reset(ws)
        args = [item if arg == ITEM else arg for arg in mutate(ws) or []]
        if not {"--upload", "--item"} & set(args):  # a later --source-id wins over this default
            args = ["--upload", tmp_path / "status.md", "--source-id", EVIDENCE_ID, *args]
        gate.connect("curator" if "--item" in args else "operator")  # only a human uploads
        local, server = judged_alike(capsys, ws, args)
        if local != server:
            mismatches.append((name, local, server))
        codes = {code for code, _path in local[3]}
        if local[0] != expected and expected not in codes:
            surprises.append((name, expected, local[0], sorted(codes)))

    assert mismatches == []
    assert surprises == []
    assert gate.jobs() == []  # a dry-run never creates a job


@pytest.mark.skipif(not LIVE_BUNDLE, reason="set AIWIKI_PARITY_BUNDLE to a clone of the live bundle")
def test_every_live_concept_is_judged_alike(tmp_path, monkeypatch, capsys) -> None:
    remote = tmp_path / "live.git"
    subprocess.run(["git", "clone", "-q", "--bare", LIVE_BUNDLE, str(remote)], check=True)
    gate = Gate(tmp_path, monkeypatch, remote=remote)
    try:
        gate.connect("operator")  # it uploads the packet
        ws = tmp_path / "ws"
        assert wiki(capsys, "workspace", "pull", "--dir", ws)[0] == 0
        concepts = sorted(rel for rel in workspace._hashes(ws, workspace=True)
                          if not rel.startswith("sources/") and should_check(ws / rel, ws))
        assert len(concepts) >= 300
        edits = (  # one per concept, in turn; a concept without a block sources list gets an uncited claim
            cite,
            lambda text: text + "\nA new, uncited claim.\n",
            lambda text: re.sub(r"(?m)^status: \w+$", "status: draft", text, count=1),  # nothing to do
            column_zero_citation,  # a YAML error where the list is indented
            lambda text: cite(text) + "\nSee [gone](nope.md).\n",
            lambda text: re.sub(r"(?m)^title: .*$", "title: Retitled", cite(text), count=1),
        )
        for index, rel in enumerate(concepts):
            text = (ws / rel).read_text(encoding="utf-8")
            change = edits[index % len(edits)] if "\nsources:\n" in text else edits[1]
            (ws / rel).write_text(change(text), encoding="utf-8")
        monkeypatch.setenv("AIWIKI_CHANGESET_MAX_FILES", str(len(concepts)))
        put(tmp_path, "status.md", EVIDENCE)

        local, server = judged_alike(capsys, ws, ["--upload", tmp_path / "status.md", "--source-id", EVIDENCE_ID])

        assert local == server
        codes = {code for code, _path in local[3]}
        assert {"uncited_change", "yaml_parse", "broken_link", "identity_locked"} <= codes
        assert len({path for _code, path in local[3]}) >= len(concepts) // 2
    finally:
        gate.close()
