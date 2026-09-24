"""The changeset gate's pure evaluation (design §2.2–§2.9, acceptance §10.1).

Concept fixtures come from production bundle a358395 (``fixtures/live_bundle``, prose
redacted, bookkeeping lines byte-identical). The failed-ingest receipts 02325bea5bc3,
3399e2a8cea8 and a658b12f157b (``fixtures/receipts``) are replayed as changesets: the
bookkeeping failures they hit are now stamped by the gate, never reported as errors.
Set ``AIWIKI_TEST_BUNDLE_GIT`` to a bundle clone to replay against the whole bundle.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiwiki.engine.document import concept_metadata, parse_document
from aiwiki.runtime import changeset
from aiwiki.runtime.changeset import EvidenceFile

FIXTURES = Path(__file__).parent / "fixtures"
LIVE = FIXTURES / "live_bundle"
ACTOR = "process:ai-wiki-maintainer"
NOW = datetime(2026, 9, 24, 1, 2, 3, 456000, tzinfo=UTC)
STAMP = "2026-09-24T01:02:03Z"
AI_STUDY = "experiments/ai-study-deferred-login-ab.md"
AIO_AB = "experiments/web-landing-page-aio-ab.md"
METRIC = "metrics/plugin-install-first-payment-funnel-2026-09.md"
EVIDENCE = b"# Daily evidence 2026-09-20\n\nRedacted evidence body.\n"
EVIDENCE_ID = "dated-source-20260920-9549"


def _receipt(job: str) -> dict:
    return json.loads((FIXTURES / "receipts" / f"{job}.json").read_text(encoding="utf-8"))


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    shutil.copytree(LIVE, root)
    (root / "index.md").write_text('---\nokf_version: "0.2"\n---\n# Bundle\n', encoding="utf-8")
    for path in sorted(root.rglob("*.md")):
        for resource in re.findall(r"resource: (/sources/\S+)", path.read_text(encoding="utf-8")):
            stub = root / resource.lstrip("/")
            stub.parent.mkdir(parents=True, exist_ok=True)
            stub.write_bytes(f"frozen evidence {stub.name}\n".encode())
    return root


def _read(bundle: Path, rel: str) -> str:
    return (bundle / rel).read_text(encoding="utf-8")


def _put(bundle: Path, rel: str, content: str, base: object = "head") -> dict:
    if base == "head":
        base = changeset.content_hash(_read(bundle, rel)) if (bundle / rel).is_file() else None
    return {"path": rel, "op": "put", "base": base, "content": content}


def _request(*files: dict, evidence: dict | None = None, **extra) -> dict:
    upload = {"filename": "evidence.md", "content_b64": base64.b64encode(EVIDENCE).decode()}
    return {
        "schema": changeset.SCHEMA, "kind": "curate", "intent": "evidence", "base_revision": "a3583950c1",
        "work_items": [], "evidence": evidence or {"id": EVIDENCE_ID, "upload": upload},
        "files": list(files), **extra,
    }


def _evaluate(bundle: Path, request: dict, **kwargs) -> dict:
    return changeset.evaluate(bundle, request, actor=ACTOR, now=NOW, **kwargs)


def _cite(text: str, *, entry: str | None = None, evidence_id: str = EVIDENCE_ID) -> str:
    """Append one packet citation to ``sources`` (in the list's own indentation) and a claim."""
    lines = text.splitlines(keepends=True)
    start = lines.index("sources:\n")
    end = next(i for i in range(start + 1, len(lines)) if re.match(r"[A-Za-z_]+:|---", lines[i]))
    pad = re.match(r" *", lines[start + 1]).group(0)
    lines.insert(end, entry or f"{pad}- {{id: {evidence_id}, resource: evidence:packet}}\n")
    return "".join(lines).replace("# Summary\n\n", f"# Summary\n\nNew dated claim.[^{evidence_id}]\n\n", 1)


def _new_concept(title: str = "H5 recovery acceptance", *, extra: str = "", body: str = "Accepted.") -> str:
    return (
        f"---\ntype: Risk\ntitle: {title}\ndescription: New risk\ntags: [web, payment]\n{extra}"
        f"sources:\n  - id: {EVIDENCE_ID}\n    resource: evidence:packet\n---\n# Summary\n\n"
        f"{body}[^{EVIDENCE_ID}]\n"
    )


def _codes(result: dict) -> list[str]:
    return [error["code"] for error in result["errors"]]


def _tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_the_gate_imports_without_the_codex_runner_or_service_extras() -> None:
    script = (
        "import sys, aiwiki.runtime.changeset; "
        "print(sorted(m for m in ('aiwiki.runtime.curate', 'aiwiki.runtime.config', 'fastapi') if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        text=True, capture_output=True, timeout=30, check=True,
    )
    assert result.stdout.strip() == "[]"


# --- hashes ------------------------------------------------------------------------------


def test_content_hash_ignores_service_keys_and_whitespace_only() -> None:
    doc = "---\ntype: Risk\ntitle: T\nstatus: draft\ngenerated: {by: a/b, at: 2026-01-01T00:00:00Z}\n---\n# X\n"
    same = (
        "---\ntitle: T\ntype: Risk\nstatus: stable\ngenerated: {by: c/d, at: 2026-02-02T00:00:00Z}\n"
        "verified: [{by: e/f, at: 2026-02-02T00:00:00Z}]\n---\n# X   \n\n\n"
    )
    assert changeset.content_hash(doc) == changeset.content_hash(same)
    assert changeset.content_hash(doc) != changeset.content_hash(doc.replace("# X", "# Y"))
    assert changeset.content_hash(doc) != changeset.content_hash(doc.replace("title: T", "title: U"))
    # Pinned: the CLI and the gate must agree across releases.
    assert changeset.content_hash(doc) == "ch1:cbbe5afcfe9f594b375575055a309be7dc443e41e973ef3508fd428846213780"
    broken = "---\ntype: [\n---\nbody\n"
    assert changeset.content_hash(broken) == changeset.content_hash(broken + "\n\n")


def test_changeset_sha256_keys_content_and_evidence_not_base_run_or_message(bundle: Path) -> None:
    first = _request(_put(bundle, METRIC, "a"), _put(bundle, AI_STUDY, "b"), run="WAIO-1", message="m")
    reordered = _request(_put(bundle, AI_STUDY, "b", base=None), _put(bundle, METRIC, "a", base=None))
    reordered["base_revision"] = "b71c2e0"
    key = changeset.changeset_sha256(first)
    assert changeset.changeset_sha256(reordered) == key
    assert changeset.changeset_sha256(_request(_put(bundle, METRIC, "a!"), _put(bundle, AI_STUDY, "b"))) != key
    other = _request(_put(bundle, METRIC, "a"), _put(bundle, AI_STUDY, "b"), evidence={
        "id": EVIDENCE_ID, "upload": {"filename": "evidence.md", "content_b64": base64.b64encode(b"x").decode()},
    })
    assert changeset.changeset_sha256(other) != key
    items = _request(_put(bundle, METRIC, "a"), evidence={"id": EVIDENCE_ID, "item_files": ["it_1/S1.md"]})
    assert changeset.changeset_sha256(items, {"it_1/S1.md": "0" * 64}) != changeset.changeset_sha256(
        items, {"it_1/S1.md": "1" * 64},
    )
    assert key == "db99c84f8f28a1169e5a447eb9527de8ed08764c909a96b36fe15e62092d1143"


# --- G1: schema, limits, paths -------------------------------------------------------------


SCHEMA_ERRORS = {
    "not_a_mapping": lambda request: ["curate"],
    "wrong_schema": lambda request: {**request, "schema": "ai-wiki.changeset/v2"},
    "unknown_field": lambda request: {**request, "priority": 1},
    "bad_base_revision": lambda request: {**request, "base_revision": "HEAD"},
    "restructure_off": lambda request: {**request, "intent": "restructure"},
    "two_evidence_kinds": lambda request: {**request, "evidence": {**request["evidence"], "item_files": ["it_1/a"]}},
    "item_not_listed": lambda request: {**request, "evidence": {"id": "x", "item_files": ["it_1/a.md"]}},
    "bad_base64": lambda request: {**request, "evidence": {"id": "x", "upload": {"filename": "a", "content_b64": "%"}}},
    "evidence_id_with_dot": lambda request: {**request, "evidence": {**request["evidence"], "id": "a.b"}},
    "bad_base_hash": lambda request: {**request, "files": [{**request["files"][0], "base": "sha:1"}]},
    "duplicate_path": lambda request: {**request, "files": request["files"] * 2},
    "deprecate_without_reason": lambda request: {**request, "files": [{
        "path": METRIC, "op": "deprecate", "base": request["files"][0]["base"], "superseded_by": AI_STUDY,
        "reason": " ",
    }]},
    # Unhashable or undecodable values are input errors, never exceptions.
    "kind_list": lambda request: {**request, "kind": ["curate"]},
    "kind_mapping": lambda request: {**request, "kind": {}},
    "op_list": lambda request: {**request, "files": [{**request["files"][0], "op": ["put"]}]},
    "lone_surrogate": lambda request: {**request, "files": [{**request["files"][0], "content": "x\ud800"}]},
    "upload_name_with_newline": lambda request: {**request, "evidence": {**request["evidence"], "upload": {
        **request["evidence"]["upload"], "filename": "evidence\n.md"}}},
}


@pytest.mark.parametrize("case", sorted(SCHEMA_ERRORS))
def test_malformed_requests_are_input_errors(bundle: Path, case: str) -> None:
    request = SCHEMA_ERRORS[case](_request(_put(bundle, METRIC, _read(bundle, METRIC))))
    result = _evaluate(bundle, request)
    assert (result["status"], result["http_status"]) == ("rejected", 400)
    assert set(_codes(result)) == {"input"}
    assert result["failure"]["class"] == "input" and result["failure"]["retryable"] is False


@pytest.mark.parametrize("name", ["it_1/..", "it_1/.", "it_1/a\nb.md", "it_1/a/b.md"])
def test_item_files_name_one_plain_file(name: str) -> None:
    put = {"path": "risks/x.md", "op": "put", "base": None, "content": "c"}
    request = _request(put, work_items=["it_1"], evidence={"id": "x", "item_files": [name]})
    assert _codes({"errors": changeset.check_request(request)}) == ["input"]
    assert changeset.check_request({**request, "evidence": {"id": "x", "item_files": ["it_1/S1-README.md"]}}) == []


def test_limits_are_413_and_tunable(bundle: Path, monkeypatch) -> None:
    many = [_put(bundle, f"risks/r{i}.md", _new_concept(f"Risk {i}")) for i in range(21)]
    assert _codes(_evaluate(bundle, _request(*many))) == ["too_large"]
    big = _put(bundle, "risks/big.md", _new_concept(body="x" * (128 * 1024)))
    assert _evaluate(bundle, _request(big))["http_status"] == 413
    monkeypatch.setenv("AIWIKI_CHANGESET_MAX_PACKET_TEXT_BYTES", "16")
    result = _evaluate(bundle, _request(_put(bundle, "risks/a.md", _new_concept())))
    assert (result["http_status"], _codes(result)) == (413, ["too_large"])
    monkeypatch.setenv("AIWIKI_CHANGESET_MAX_FILES", "1")
    assert _evaluate(bundle, _request(*many[:2]))["http_status"] == 413


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("sources/new.md", "service_owned_path"),
        ("SCHEMA.md", "service_owned_path"),
        ("purpose.md", "service_owned_path"),
        ("index.md", "service_owned_path"),
        ("experiments/index.md", "service_owned_path"),
        ("log.md", "service_owned_path"),
        ("index-meta.yaml", "service_owned_path"),
        (".okf/jobs/x.md", "service_owned_path"),
        ("../outside.md", "path_forbidden"),
        ("/abs.md", "path_forbidden"),
        ("risks//x.md", "path_forbidden"),
        (".git/x.md", "path_forbidden"),
        ("notes.txt", "path_forbidden"),
        (METRIC + "/child.md", "path_forbidden"),  # through an existing concept file
        ("risks/" + "a" * 201 + ".md", "path_forbidden"),  # longer than a file name may be
        ("risks/a\nb.md", "path_forbidden"),  # would forge a log.md line
        ("risks/a\u2028b.md", "path_forbidden"),
    ],
)
def test_path_rules(bundle: Path, path: str, code: str) -> None:
    result = _evaluate(bundle, _request(_put(bundle, path, _new_concept(), base=None)))
    assert (result["http_status"], _codes(result)) == (422, [code])


def test_symlinks_case_collisions_deletes_and_nfc(bundle: Path) -> None:
    (bundle / "risks").mkdir()
    (bundle / "risks" / "link.md").symlink_to(bundle / METRIC)
    linked = _evaluate(bundle, _request(_put(bundle, "risks/link.md", _new_concept(), base=None)))
    assert _codes(linked) == ["path_forbidden"]
    for upper in (METRIC.replace("metrics/", "Metrics/"), "Metrics/new.md"):
        result = _evaluate(bundle, _request(_put(bundle, upper, _new_concept(), base=None)))
        assert _codes(result) == ["path_forbidden"] and "only in case" in result["errors"][0]["message"]
    twins = _request(_put(bundle, "risks/a.md", _new_concept(), base=None),
                     _put(bundle, "Risks/b.md", _new_concept("Other"), base=None))
    assert _codes(_evaluate(bundle, twins)) == ["path_forbidden"]
    nested = _request(_put(bundle, "risks/a.md", _new_concept(), base=None),
                      _put(bundle, "risks/a.md/b.md", _new_concept("Other"), base=None))
    assert [(error["code"], error["path"]) for error in _evaluate(bundle, nested)["errors"]] == [
        ("path_forbidden", "risks/a.md/b.md"),
    ]
    deleted = _evaluate(bundle, _request({"path": METRIC, "op": "delete"}))
    assert _codes(deleted) == ["delete_forbidden"]

    decomposed = unicodedata.normalize("NFD", "risks/café-risk.md")
    result = _evaluate(bundle, _request(_put(bundle, decomposed, _new_concept(), base=None)))
    assert result["status"] == "would_apply", result["errors"]
    assert result["concept_files"] == [unicodedata.normalize("NFC", decomposed)]
    assert {"path": "risks/café-risk.md", "key": "path", "from": decomposed, "to": "risks/café-risk.md",
            "action": "nfc_normalized"} in result["normalizations"]


# --- G6: per-file CAS --------------------------------------------------------------------


def test_stale_missing_and_existing_bases_conflict(bundle: Path) -> None:
    current = changeset.content_hash(_read(bundle, METRIC))
    stale = _evaluate(bundle, _request(_put(bundle, METRIC, _cite(_read(bundle, METRIC)), base="ch1:" + "0" * 64)))
    assert (stale["http_status"], _codes(stale)) == (409, ["conflict"])
    assert stale["conflicts"] == [{"path": METRIC, "base": "ch1:" + "0" * 64, "current": current}]
    assert stale["failure"]["class"] == "conflict"
    exists = _evaluate(bundle, _request(_put(bundle, METRIC, _new_concept(), base=None)))
    assert exists["conflicts"] == [{"path": METRIC, "base": None, "current": current}]
    missing = _evaluate(bundle, _request(_put(bundle, "risks/none.md", _new_concept(), base=current)))
    assert missing["conflicts"] == [{"path": "risks/none.md", "base": current, "current": None}]


def test_an_audit_stamp_does_not_invalidate_a_curator_base(bundle: Path) -> None:
    base = changeset.content_hash(_read(bundle, METRIC))
    audited = _read(bundle, METRIC).replace(
        "verified:\n", "verified:\n- {by: process:ai-wiki-auditor, at: 2026-09-23T00:00:00Z}\n",
    ).replace("status: stable", "status: draft")
    (bundle / METRIC).write_text(audited, encoding="utf-8")
    result = _evaluate(bundle, _request(_put(bundle, METRIC, _cite(audited), base=base)))
    assert result["status"] == "would_apply", result["errors"]


# --- evidence packet ----------------------------------------------------------------------


def test_one_file_packet_is_stored_verbatim_under_the_ingest_name() -> None:
    packet, errors = changeset.build_packet(
        {"id": "evidence", "upload": {"filename": "evidence.md", "content_b64": base64.b64encode(EVIDENCE).decode()}},
    )
    assert errors == [] and packet.data == EVIDENCE and packet.filename == "evidence.md"
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    packet, errors = changeset.build_packet(
        {"id": "chart", "item_files": ["it_1/chart.png"]}, [EvidenceFile("it_1/chart.png", png, {"kind": "upload"})],
    )
    assert errors == [] and packet.data == png and packet.filename == "chart.png"


def test_several_parts_become_one_markdown_packet_with_an_origin_header() -> None:
    git_part = {"kind": "git-file", "remote": "https://code.ddit.ai/solvely-web/solvely-web-control.git",
                "commit": "1a2b3c4d5e", "path": "tasks/h5/README.md", "blob": "c7d2", "truncated": False}
    files = [
        EvidenceFile("it_3f2a/S1-README.md", b"# Task\n\nStatus: done\n", git_part),
        EvidenceFile("it_3f2a/S2-status.md", b"merged", {"kind": "multica-comment", "issue": "WAIO-587",
                                                         "comment": "01a0c068"}),
    ]
    evidence = {"id": "h5-status", "title": "H5 status", "item_files": ["it_3f2a/S1-README.md", "it_3f2a/S2-status.md"]}
    packet, errors = changeset.build_packet(evidence, files)
    assert errors == [] and packet.filename == "h5-status.md"
    assert changeset.build_packet(evidence, list(reversed(files)))[0] == packet  # deterministic
    document = parse_document(packet.data.decode())
    header = document.frontmatter
    assert (header["ai_wiki_evidence"], header["id"], header["title"]) == (1, "h5-status", "H5 status")
    assert header["parts"][0] == {"ref": "S1", "item": "it_3f2a", "file": "S1-README.md",
                                  "sha256": hashlib.sha256(files[0].data).hexdigest(), **git_part}
    assert header["parts"][1]["ref"] == "S2" and header["parts"][1]["issue"] == "WAIO-587"
    assert packet.data.decode().endswith(
        "---\n## S1 · solvely-web-control@1a2b3c4:tasks/h5/README.md\n\n# Task\n\nStatus: done\n\n"
        "## S2 · WAIO-587#01a0c068\n\nmerged\n"
    )
    binary = [*files, EvidenceFile("it_3f2a/c.png", b"\xff\xfe\x00", {})]
    _packet, errors = changeset.build_packet({**evidence, "item_files": [f.name for f in binary]}, binary)
    assert _codes({"errors": errors}) == ["input"]
    _packet, errors = changeset.build_packet(evidence, files[:1])
    assert "not provided" in errors[0]["message"]


def test_item_files_packet_through_evaluate(bundle: Path) -> None:
    files = [EvidenceFile("it_3f2a/S1-README.md", b"# Task\n\nDone.\n", {"kind": "git-file"}),
             EvidenceFile("it_3f2a/S2-status.md", b"Merged.\n", {"kind": "git-file"})]
    request = _request(_put(bundle, "risks/h5.md", _new_concept(), base=None), work_items=["it_3f2a"],
                       evidence={"id": EVIDENCE_ID, "item_files": [item.name for item in files]})
    result = _evaluate(bundle, request, evidence_files=files)
    assert result["status"] == "would_apply", result["errors"]
    assert result["evidence"] == {"id": EVIDENCE_ID, "parts": 2, "origin_kinds": ["git-file"]}
    assert result["source"].startswith(f"sources/{EVIDENCE_ID}-") and result["source"].endswith(".md.source")
    assert result["changeset_sha256"] == changeset.changeset_sha256(
        request, {item.name: hashlib.sha256(item.data).hexdigest() for item in files},
    )


# --- real failed receipts replayed as changesets -----------------------------------------


def test_3399e2a8cea8_packet_references_cannot_be_miscopied(bundle: Path) -> None:
    """3399e2a8cea8 mistyped a 64-hex snapshot path; a changeset cites ``evidence:packet``."""
    receipt = _receipt("3399e2a8cea8")
    typos = dict(re.findall(r"^(\S+\.md): \S+ does not resolve to a local file: '(\S+)'",
                            "\n".join(receipt["validation"]["errors"]), re.M))
    created = sorted(typos)
    request = _request(*(_put(bundle, rel, _new_concept(f"H5 {i}"), base=None) for i, rel in enumerate(created)))
    result = _evaluate(bundle, request)
    assert result["status"] == "would_apply", result["errors"]
    snapshot = "/" + result["source"]
    assert result["normalizations"] == [
        {"path": rel, "key": "sources[0].resource", "from": "evidence:packet", "to": snapshot} for rel in created
    ]
    for rel in created:
        frontmatter = parse_document(result["files"][rel]).frontmatter
        assert frontmatter["sources"][0]["resource"] == snapshot
        assert frontmatter["status"] == "draft"
        assert frontmatter["generated"] == {"by": ACTOR, "at": NOW.replace(microsecond=0)}
        assert "verified" not in frontmatter
    assert result["bookkeeping_preview"] == {rel: ["generated stamped", "status=draft"] for rel in created}

    # The receipt's own miscopied references: the gate reports them, never guesses.
    miscopied = _request(*(
        _put(bundle, rel, _new_concept(f"H5 {i}").replace("evidence:packet", typos[rel]), base=None)
        for i, rel in enumerate(created)
    ))
    replay = _evaluate(bundle, miscopied)
    receipt_codes = sorted(changeset.error_code(error) for error in receipt["validation"]["errors"])
    assert sorted(_codes(replay)) == receipt_codes == ["resource_unresolvable"] * 2 + ["uncited_change"] * 2


def test_02325bea5bc3_column_zero_source_item_is_located_and_the_fixed_edit_is_stamped(bundle: Path) -> None:
    """02325bea5bc3 appended a sources item at column 0 after a nested usage_window."""
    receipt = _receipt("02325bea5bc3")
    assert all("expected <block end>, but found '-'" in error for error in receipt["validation"]["errors"])
    column_zero = f"- id: {EVIDENCE_ID}\n  resource: evidence:packet\n"
    replayed = sorted({error.split(":", 1)[0] for error in receipt["validation"]["errors"]} & {AI_STUDY, AIO_AB})
    broken = {rel: _cite(_read(bundle, rel), entry=column_zero) for rel in replayed}
    result = _evaluate(bundle, _request(*(_put(bundle, rel, text) for rel, text in broken.items())))
    assert result["errors"] == [{
        "code": "yaml_parse", "path": rel, "line": text.splitlines().index(f"- id: {EVIDENCE_ID}") + 1,
        "column": 1, "message": "expected <block end>, but found '-'",
        "hint": "indent this list item like the items above it",
    } for rel, text in broken.items()]
    assert (result["http_status"], result["failure"]["class"], result["failure"]["retryable"]) == (
        422, "model_output", True,
    )

    # The fixed edit also forges every service key; the gate overrides, it does not reject.
    head = _read(bundle, AI_STUDY)
    fixed = _cite(head).replace("status: stable", "status: draft").replace(
        "  at: '2026-09-23T15:46:00Z'", "  at: '2026-09-20T20:15:00Z'",
    ).replace("verified:\n", "verified:\n  - {by: 'human:owner', at: 2026-09-24T00:00:00Z}\n")
    result = _evaluate(bundle, _request(_put(bundle, AI_STUDY, fixed)))
    assert result["status"] == "would_apply", result["errors"]
    assert result["warnings"] == [
        {"code": "service_owned_key_ignored", "path": AI_STUDY, "key": key}
        for key in ("generated", "status", "verified")
    ]
    frontmatter = parse_document(result["files"][AI_STUDY]).frontmatter
    before = parse_document(head).frontmatter
    assert frontmatter["status"] == "stable" and frontmatter["verified"] == before["verified"]
    assert frontmatter["generated"] == {"by": ACTOR, "at": STAMP}
    assert concept_metadata(frontmatter)["verification_current"] is False
    assert result["deterministic_repairs"] == {AI_STUDY: [
        "restored service-owned verification history without adding verification", "restored service-owned status",
    ]}


def test_a658b12f157b_same_evidence_is_judged_without_any_process(bundle: Path, monkeypatch) -> None:
    """a658b12f157b timed out after 900s in the Codex agent; the gate never starts a process."""
    receipt = _receipt("a658b12f157b")
    assert receipt["error"] == "curation timed out after 900s"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the changeset gate must not start a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    edits = [_put(bundle, rel, _cite(_read(bundle, rel), evidence_id="evidence")) for rel in (AI_STUDY, METRIC)]
    result = _evaluate(bundle, _request(*edits, evidence={"id": "evidence", "upload": {
        "filename": receipt["original_name"], "content_b64": base64.b64encode(EVIDENCE).decode(),
    }}))
    assert result["status"] == "would_apply", result["errors"]
    assert result["source"] == f"sources/evidence-{hashlib.sha256(EVIDENCE).hexdigest()}.md.source"
    assert result["concept_files"] == sorted([AI_STUDY, METRIC])


# --- error keys and codes ----------------------------------------------------------------


def test_error_keys_ignore_numbers_and_codes_cover_the_engine_messages() -> None:
    assert changeset.error_key("a/b.md: sources[3].id duplicates 'x-20260920'") == (
        "a/b.md", "sources[#].id duplicates 'x-#'",
    )
    assert changeset.error_key("missing root index.md") == ("", "missing root index.md")
    messages = {
        "invalid YAML frontmatter: mapping values are not allowed here": "yaml_parse",
        "body starts with 1 spilled frontmatter/verification line(s), first 'x'": "yaml_parse",
        "missing required frontmatter key description": "missing_key",
        "tags must be a non-empty list of strings": "invalid_value",
        "sources[0].id duplicates 'a'": "invalid_value",
        "legacy frontmatter key last_verified_at is not supported": "legacy_key",
        "invalid status 'reviewed'; expected draft|stable|deprecated (legacy status is not supported)":
            "invalid_status",
        "sources[0].resource does not resolve to a local file: '/sources/x' -> 'sources/x'":
            "resource_unresolvable",
        "changed concepts must cite current ingest snapshot 'sources/x'": "uncited_change",
        "unresolved link: ../risks/none.md": "broken_link",
        "contradictions target missing: none.md": "dangling_contradiction",
        "curation deleted a concept; deprecate it instead": "delete_forbidden",
        "curation modified immutable source evidence": "service_owned_path",
        "curation modified a prohibited non-concept bundle file": "path_forbidden",
        "new concepts must set generated.by to 'process:x'": "validation",
        "something new": "validation",
    }
    assert {message: changeset.error_code(message) for message in messages} == messages
    assert set(changeset.CODES) >= set(messages.values()) | {"unknown_evidence_ref", "duplicate_title"}


# --- G9-G12 on real concepts --------------------------------------------------------------


def _new(bundle: Path, content: str, rel: str = "risks/new.md") -> dict:
    return _evaluate(bundle, _request(_put(bundle, rel, content, base=None)))


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (_new_concept().replace("description: New risk\n", ""), "missing_key"),
        (_new_concept().replace("tags: [web, payment]", "tags: web"), "invalid_value"),
        (_new_concept(extra="last_verified_at: 2026-09-01T00:00:00Z\n"), "legacy_key"),
        (_new_concept().replace("resource: evidence:packet", "resource: evidence:S1"), "unknown_evidence_ref"),
        (_new_concept().replace(f"id: {EVIDENCE_ID}", "id: other"), "unknown_evidence_ref"),
        (_new_concept().replace("resource: evidence:packet", "resource: /sources/none.md.source"),
         "resource_unresolvable"),
        (_new_concept(body="See [gone](../metrics/gone.md)."), "broken_link"),
        (_new_concept(extra="contested: true\ncontradictions: [../metrics/gone.md]\n"), "dangling_contradiction"),
        (_new_concept("Redacted title"), "duplicate_title"),
        (_new_concept(extra="aliases: [redacted-ALIAS 1]\n"), "duplicate_title"),
        (_new_concept(body="key AKIA" + "ABCDEFGHIJKLMNOP"), "secret_detected"),
    ],
)
def test_each_gate_error_code(bundle: Path, content: str, code: str) -> None:
    result = _new(bundle, content)
    assert code in _codes(result), result["errors"]
    assert result["http_status"] == 422


def test_forged_service_keys_on_a_new_concept_are_overwritten_not_rejected(bundle: Path) -> None:
    forged = _new_concept(extra=(
        "status: reviewed\ngenerated: {by: 'human:x', at: 2030-01-01T00:00:00Z}\n"
        "verified: [{by: 'human:x', at: 2030-01-01T00:00:00Z}]\n"
    ))
    result = _new(bundle, forged)
    assert result["status"] == "would_apply", result["errors"]
    assert [warning["key"] for warning in result["warnings"]] == ["generated", "status", "verified"]
    frontmatter = parse_document(result["files"]["risks/new.md"]).frontmatter
    assert (frontmatter["status"], frontmatter["generated"]["by"], "verified" in frontmatter) == (
        "draft", ACTOR, False,
    )
    assert concept_metadata(frontmatter)["trust"] == "unverified"


def test_uncited_change_and_secrets_never_echo_the_value(bundle: Path) -> None:
    head = _read(bundle, METRIC)
    result = _evaluate(bundle, _request(_put(bundle, METRIC, head + "\nAdded claim.\n")))
    assert _codes(result) == ["uncited_change"]
    assert result["errors"][0]["hint"].startswith("cite {id: <evidence.id>, resource: evidence:packet}")
    secret = "ghp_" + "A" * 36
    leaked = _new(bundle, _new_concept(body=f"token {secret}"))
    assert leaked["errors"] == [{
        "code": "secret_detected", "path": "risks/new.md", "line": 12, "rule": "github_token",
        "message": "content matches secret rule github_token", "hint": changeset.HINTS["secret_detected"],
    }]
    assert secret not in json.dumps(leaked)
    # Parked, not counted: the same bytes would fail every retry (§2.8).
    assert (leaked["failure"]["class"], leaked["failure"]["retryable"]) == ("input", False)

    # Repairs and other errors quote the agent's lines; a rejection redacts them.
    spill = f"---\ndescription: deploy with {secret}\n# Summary"
    spilled = _new(bundle, _new_concept().replace("---\n# Summary", spill))
    assert _codes(spilled) == ["secret_detected"] and secret not in json.dumps(spilled)
    assert spilled["deterministic_repairs"]["risks/new.md"] == [
        "discarded spilled frontmatter line written by editor: 'description: deploy with <redacted:github_token>'",
    ]
    linked = _new(bundle, _new_concept(body=f"See [creds]({secret}.md)."))
    assert sorted(_codes(linked)) == ["broken_link", "secret_detected"] and secret not in json.dumps(linked)
    assert "unresolved link: <redacted:github_token>.md" in [error["message"] for error in linked["errors"]]

    upload = {"filename": "evidence.md", "content_b64": base64.b64encode(f"# E\n\n{secret}\n".encode()).decode()}
    packet = _evaluate(bundle, _request(_put(bundle, "risks/new.md", _new_concept(), base=None),
                                        evidence={"id": EVIDENCE_ID, "upload": upload}))
    assert [(error["code"], error["line"]) for error in packet["errors"]] == [("secret_detected", 3)]
    assert (packet["failure"]["class"], packet["failure"]["retryable"]) == ("input", False)


def test_a_secret_in_the_message_or_run_is_rejected(bundle: Path) -> None:
    """Both become the commit message, and redaction cannot reach key material after a
    bare PEM header: reject instead, before anything reaches Git."""
    secret = "ghp_" + "A" * 36
    header = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA7bq9Zx1Qk3LmNoPqRsTuVwXyZ"
    change = _put(bundle, METRIC, _cite(_read(bundle, METRIC)))

    result = _evaluate(bundle, _request(change, message=header, run=f"WAIO-612 {secret}"))

    assert [(error["code"], error["rule"]) for error in result["errors"]] == [
        ("secret_detected", "private_key"), ("secret_detected", "github_token"),
    ]
    assert [error["message"] for error in result["errors"]] == [
        "message matches secret rule private_key", "run matches secret rule github_token",
    ]
    assert secret not in json.dumps(result) and "MIIE" not in json.dumps(result)
    assert (result["failure"]["class"], result["failure"]["retryable"]) == ("input", False)


def test_identity_and_body_shrink_guards_honor_allow(bundle: Path) -> None:
    head = _read(bundle, AI_STUDY)
    retitled = _cite(head).replace("title: Redacted title", "title: Renamed", 1)
    assert _codes(_evaluate(bundle, _request(_put(bundle, AI_STUDY, retitled)))) == ["identity_locked"]
    allowed = _request(_put(bundle, AI_STUDY, retitled), allow={"retype": [{"path": AI_STUDY, "reason": "rename"}]})
    assert _evaluate(bundle, allowed)["status"] == "would_apply"

    shrunk = _cite(head[: head.index("# Entry and handoff contract")])
    assert _codes(_evaluate(bundle, _request(_put(bundle, AI_STUDY, shrunk)))) == ["body_shrink"]
    allowed = _request(_put(bundle, AI_STUDY, shrunk), allow={"shrink": [{"path": AI_STUDY, "reason": "moved"}]})
    assert _evaluate(bundle, allowed)["status"] == "would_apply"


def test_dropped_sources_are_restored_not_rejected(bundle: Path) -> None:
    head = _read(bundle, METRIC)
    start = head.index("- id: dated-first-payment")
    dropped = _cite(head[:start] + head[head.index("aliases:"):], entry=f"- {{id: {EVIDENCE_ID}, "
                    "resource: evidence:packet}\n")
    result = _evaluate(bundle, _request(_put(bundle, METRIC, dropped)))
    assert result["status"] == "would_apply", result["errors"]
    resources = [source["resource"] for source in parse_document(result["files"][METRIC]).frontmatter["sources"]]
    assert resources[0] == "/" + result["source"] and resources[1].startswith("/sources/8a76e184")
    assert {"path": METRIC, "key": "sources", "action": "restored_from_head", "resources": [resources[1]]} in (
        result["normalizations"]
    )


def _break_verified(text: str) -> str:
    """Mis-indent METRIC's verified list: YAML the gate discards along with the block."""
    event = "- {by: process:ai-wiki-adversarial-audit, at: 2026-09-17T21:28:57Z}\n"
    return text.replace(f"verified:\n{event}", f"verified:\n  {event} - {{by: 'human:x', at: 2026-09-24T00:00:00Z}}\n")


def test_broken_service_blocks_are_discarded_before_the_packet_is_cited(bundle: Path) -> None:
    head = _read(bundle, METRIC)
    edit = _break_verified(_cite(head))
    assert changeset._frontmatter(edit) is None  # the verified block no longer parses
    result = _evaluate(bundle, _request(_put(bundle, METRIC, edit)))
    assert result["status"] == "would_apply", result["errors"]
    assert "evidence:packet" not in result["files"][METRIC]
    frontmatter = parse_document(result["files"][METRIC]).frontmatter
    assert frontmatter["sources"][-1] == {"id": EVIDENCE_ID, "resource": "/" + result["source"]}
    assert frontmatter["verified"] == parse_document(head).frontmatter["verified"]
    assert result["warnings"] == [{"code": "service_owned_key_ignored", "path": METRIC, "key": "verified"}]

    new = _new(bundle, _new_concept(extra="generated:\n  by: me\n at: 2026-09-24T00:00:00Z\n"))
    assert new["status"] == "would_apply", new["errors"]
    frontmatter = parse_document(new["files"]["risks/new.md"]).frontmatter
    assert frontmatter["status"] == "draft"
    assert frontmatter["generated"] == {"by": ACTOR, "at": NOW.replace(microsecond=0)}

    # Sources stay append-only: citing the packet by its final path does not drop HEAD's.
    packet = f"/sources/{EVIDENCE_ID}-{hashlib.sha256(EVIDENCE).hexdigest()}.md.source"
    head_resource = parse_document(head).frontmatter["sources"][0]["resource"]
    replaced = head[: head.index("sources:\n")] + f"sources:\n- id: {EVIDENCE_ID}\n  resource: {packet}\n" + head[
        head.index("aliases:\n"):
    ]
    replaced = _break_verified(replaced).replace("# Summary\n\n", f"# Summary\n\nNew claim.[^{EVIDENCE_ID}]\n\n", 1)
    result = _evaluate(bundle, _request(_put(bundle, METRIC, replaced)))
    assert result["status"] == "would_apply", result["errors"]
    sources = parse_document(result["files"][METRIC]).frontmatter["sources"]
    assert [source["resource"] for source in sources] == [packet, head_resource]
    assert {"path": METRIC, "key": "sources", "action": "restored_from_head", "resources": [head_resource]} in (
        result["normalizations"]
    )


@pytest.mark.parametrize(
    ("rel", "line", "key"),
    [
        (AIO_AB, '"stat\\x75s": stable\n', "status"),  # would promote a draft without an audit
        (METRIC, '"stat\\x75s": deprecated\n', "status"),  # would deprecate without superseded_by
        (METRIC, "!!str status: draft\n", "status"),
        (METRIC, '"s\\x6furces": []\n', "sources"),  # would drop append-only sources
        (METRIC, '"gener\\x61ted": {by: "process:ai-wiki-maintainer", at: 2030-01-01T00:00:00Z}\n', "generated"),
    ],
)
def test_keys_yaml_reads_under_another_name_are_rejected(bundle: Path, rel: str, line: str, key: str) -> None:
    edit = _cite(_read(bundle, rel)).replace("aliases:\n", line + "aliases:\n", 1)
    result = _evaluate(bundle, _request(_put(bundle, rel, edit)))
    assert result["errors"] == [{
        "code": "yaml_parse", "path": rel, "line": edit.splitlines().index(line.rstrip("\n")) + 1,
        "hint": "write each top-level key once as a plain name: no escapes, tags, '?' or '<<'",
        "message": f"frontmatter key {key!r} is not written as a plain name",
    }]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("2026-02-30", "day is out of range for month"),
        ("!!bool maybe", "its tag does not fit the value"),
        ("!!timestamp soon", "its tag does not fit the value"),
    ],
)
def test_values_yaml_cannot_construct_are_located(bundle: Path, value: str, message: str) -> None:
    for rel, text in ((METRIC, _cite(_read(bundle, METRIC))), ("risks/new.md", _new_concept(
        extra="last_modified: '2026-09-17'\n",
    ))):
        edit = text.replace("last_modified: '2026-09-17'", f"last_modified: {value}")
        line = next(number for number, text_ in enumerate(edit.splitlines(), 1) if f"last_modified: {value}" in text_)
        result = _evaluate(bundle, _request(_put(bundle, rel, edit)))
        assert result["errors"] == [{
            "code": "yaml_parse", "path": rel, "line": line, "column": edit.splitlines()[line - 1].index(value) + 1,
            "hint": "write a real date or value, or quote it as a string",
            "message": f"YAML cannot read this value: {message}",
        }]


def test_an_evidence_id_a_touched_concept_already_cites_is_rejected(bundle: Path) -> None:
    head = _read(bundle, METRIC)
    reused = parse_document(head).frontmatter["sources"][0]["id"]
    upload = {"filename": "evidence.md", "content_b64": base64.b64encode(EVIDENCE).decode()}
    deprecate = {"path": METRIC, "op": "deprecate", "base": changeset.content_hash(head), "superseded_by": AI_STUDY,
                 "reason": "merged"}
    for entry in (deprecate, _put(bundle, METRIC, _cite(head, evidence_id=reused))):
        result = _evaluate(bundle, _request(entry, evidence={"id": reused, "upload": upload}))
        assert result["errors"] == [{
            "code": "invalid_value", "path": METRIC, "hint": "choose an evidence.id this concept's sources do not use",
            "message": f"evidence.id {reused!r} already names another source of this concept",
        }]

    # A deprecation cites the packet unless the concept already cites that very file.
    packet = f"sources/{EVIDENCE_ID}-{hashlib.sha256(EVIDENCE).hexdigest()}.md.source"
    (bundle / packet).write_bytes(EVIDENCE)
    cited = head.replace("aliases:\n", f"- {{id: {EVIDENCE_ID}, resource: /{packet}}}\naliases:\n", 1)
    (bundle / METRIC).write_text(cited, encoding="utf-8")
    result = _evaluate(bundle, _request({**deprecate, "base": changeset.content_hash(cited)}))
    assert result["status"] == "would_apply", result["errors"]
    resources = [source["resource"] for source in parse_document(result["files"][METRIC]).frontmatter["sources"]]
    assert resources.count("/" + packet) == 1


def test_only_new_validation_errors_block(bundle: Path) -> None:
    broken = _read(bundle, METRIC).replace("last_modified: '2026-09-17'", "last_modified: '2026-9-17'")
    (bundle / METRIC).write_text(broken, encoding="utf-8")
    result = _evaluate(bundle, _request(_put(bundle, AI_STUDY, _cite(_read(bundle, AI_STUDY)))))
    assert result["status"] == "would_apply", result["errors"]
    assert result["validation"] == {"status": "passed", "new_errors": 0, "baseline_errors": 1}
    assert result["warnings"] == [{"code": "baseline_error_untouched", "path": METRIC,
                                   "message": "sources[0].last_modified must be YYYY-MM-DD"}]
    # Touching the broken file makes its error the changeset's own.
    touched = _evaluate(bundle, _request(_put(bundle, METRIC, _cite(broken))))
    assert _codes(touched) == ["invalid_value"]
    assert touched["validation"] == {"status": "failed", "new_errors": 1, "baseline_errors": 0}


def test_deprecate_writes_a_dated_note_status_and_citation(bundle: Path) -> None:
    successor = "decisions/plugin-funnel.md"
    base = changeset.content_hash(_read(bundle, METRIC))
    request = _request(
        _put(bundle, successor, _new_concept("Plugin funnel decision"), base=None),
        {"path": METRIC, "op": "deprecate", "base": base, "superseded_by": successor, "reason": "merged\ninto it"},
    )
    result = _evaluate(bundle, request)
    assert result["status"] == "would_apply", result["errors"]
    assert (result["concept_files"], result["deprecated_files"]) == ([successor], [METRIC])
    document = parse_document(result["files"][METRIC])
    assert document.body.startswith(
        "> Deprecated 2026-09-24: superseded by [Plugin funnel decision](../decisions/plugin-funnel.md). "
        "merged into it\n\n# Summary\n"
    )
    assert document.frontmatter["status"] == "deprecated"
    assert document.frontmatter["sources"][-1] == {"id": EVIDENCE_ID, "resource": "/" + result["source"]}
    assert result["bookkeeping_preview"][METRIC] == ["generated stamped", "status=deprecated"]

    missing = {**request["files"][1], "superseded_by": "decisions/none.md"}
    assert _codes(_evaluate(bundle, _request(missing))) == ["broken_link"]
    (bundle / METRIC).write_text(_read(bundle, METRIC).replace("status: stable", "status: deprecated"))
    again = {**request["files"][1], "superseded_by": AI_STUDY}
    assert _codes(_evaluate(bundle, _request(again))) == ["conflict"]


def test_a_deprecation_reason_is_scanned_and_bounded(bundle: Path) -> None:
    deprecate = {"path": AI_STUDY, "op": "deprecate", "base": changeset.content_hash(_read(bundle, AI_STUDY)),
                 "superseded_by": METRIC}
    secret = "ghp_" + "A1b2" * 9
    leaked = _evaluate(bundle, _request({**deprecate, "reason": f"rotated key {secret}"}))
    assert _codes(leaked) == ["secret_detected"] and secret not in json.dumps(leaked)
    huge = _evaluate(bundle, _request({**deprecate, "reason": "x" * (2 * 1024 * 1024)}))
    assert (huge["http_status"], _codes(huge)) == (413, ["too_large"])


@pytest.mark.parametrize("successor", ["/etc/passwd", "../../etc/hosts", sys.executable, "index.md", "decisions/"])
def test_superseded_by_is_a_concept_path_before_anything_reads_it(bundle: Path, successor: str) -> None:
    deprecate = {"path": AI_STUDY, "op": "deprecate", "base": changeset.content_hash(_read(bundle, AI_STUDY)),
                 "superseded_by": successor, "reason": "merged"}
    result = _evaluate(bundle, _request(deprecate))
    assert (result["http_status"], _codes(result)) == (422, ["broken_link"])
    assert result["validation"] == {"status": "not_run"}  # rejected in G1, before any read


def test_a_symlinked_successor_is_not_a_live_concept(bundle: Path) -> None:
    (bundle / "decisions").mkdir()
    (bundle / "decisions" / "link.md").symlink_to(bundle / METRIC)
    deprecate = {"path": AI_STUDY, "op": "deprecate", "base": changeset.content_hash(_read(bundle, AI_STUDY)),
                 "superseded_by": "decisions/link.md", "reason": "merged"}
    assert _evaluate(bundle, _request(deprecate))["errors"] == [{
        "code": "broken_link", "path": AI_STUDY, "message": "superseded_by 'decisions/link.md' is not a live concept",
        "hint": changeset.HINTS["broken_link"],
    }]


def test_service_key_only_edits_are_noops_and_evaluation_is_pure(bundle: Path) -> None:
    before = _tree(bundle)
    head = _read(bundle, METRIC)
    result = _evaluate(bundle, _request(_put(bundle, METRIC, head.replace("status: stable", "status: draft"))))
    assert (result["status"], result["files"], result["noop_files"]) == ("noop", {}, [METRIC])
    _evaluate(bundle, _request(_put(bundle, AI_STUDY, _cite(_read(bundle, AI_STUDY)))))
    _evaluate(bundle, _request(_put(bundle, AI_STUDY, "not a concept")))
    assert _tree(bundle) == before


def test_audit_changesets_are_schema_checked_but_not_evaluated_here(bundle: Path) -> None:
    base = changeset.content_hash(_read(bundle, METRIC))
    audit = {"schema": changeset.SCHEMA, "kind": "audit", "base_revision": "9f1e22b0c4",
             "reviews": [{"path": METRIC, "base": base, "verdict": "verified", "note": "matches"}]}
    assert changeset.check_request(audit) == []
    assert changeset.check_request({**audit, "reviews": audit["reviews"] * 6})[0]["code"] == "too_large"
    corrected = {**audit, "reviews": [{**audit["reviews"][0], "verdict": "corrected"}]}
    assert [error["code"] for error in changeset.check_request(corrected)] == ["input"]
    assert _evaluate(bundle, audit)["http_status"] == 400


# --- whole production bundle -------------------------------------------------------------

LIVE_GIT = os.environ.get("AIWIKI_TEST_BUNDLE_GIT")


@pytest.mark.skipif(not LIVE_GIT, reason="set AIWIKI_TEST_BUNDLE_GIT to a production bundle clone")
def test_live_bundle_replay_of_the_20260920_evidence(tmp_path: Path) -> None:
    checkout = tmp_path / "bundle"
    subprocess.run(["git", "clone", "-q", LIVE_GIT, str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "a358395"], check=True)
    snapshot = "sources/evidence-95498733626bce82b436b819c0181bd0a55e99db8c6305c977168b6d16764900.md.source"
    evidence = {"id": "evidence", "upload": {
        "filename": _receipt("a658b12f157b")["original_name"],
        "content_b64": base64.b64encode((checkout / snapshot).read_bytes()).decode(),
    }}
    head = (checkout / AI_STUDY).read_text(encoding="utf-8")
    edit = head.replace("aliases:\n", "  - {id: evidence, resource: evidence:packet}\naliases:\n", 1)
    edit = edit.replace("# Summary\n\n", "# Summary\n\nReplayed claim.[^evidence]\n\n", 1)
    request = _request(_put(checkout, AI_STUDY, edit), evidence=evidence)
    result = _evaluate(checkout, request)
    assert result["status"] == "would_apply", result["errors"]
    assert result["source"] == snapshot  # the same bytes map to the production snapshot
    assert result["validation"] == {"status": "passed", "new_errors": 0, "baseline_errors": 0}
    concepts = [path for path in checkout.rglob("*.md") if ".git" not in path.parts]
    assert [path for path in concepts if changeset.secrets.scan(path.read_text(encoding="utf-8"))] == []
