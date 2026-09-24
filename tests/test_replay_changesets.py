"""Historical replay (design §10.3): an ingest the Codex path committed is proposed again as a
curate changeset on its parent, and the result may differ only in service-owned bytes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from aiwiki.engine import append_log, bookkeeping, scan_sources
from aiwiki.engine.gen_indexes import generate_indexes
from aiwiki.runtime import curate

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "tests" / "fixtures" / "live_bundle"
SPEC = importlib.util.spec_from_file_location("replay_changesets", ROOT / "scripts" / "replay_changesets.py")
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)

EVIDENCE = b"# Deferred login readout 2026-09-20\n\nThe T arm registered more users.\n"
NAME = f"evidence-{hashlib.sha256(EVIDENCE).hexdigest()}.md.source"
CITE = "readout-2026-09-20"
RETITLED = "experiments/ai-study-deferred-login-ab.md"
SPILLED = "experiments/web-landing-page-aio-ab.md"  # a verification event below its closing fence
NEW = "metrics/deferred-login-readout-2026-09.md"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _codex_edit(text: str) -> str:
    """What the Codex path committed: the packet cited, a claim added, status and generated stamped."""
    lines = text.splitlines(keepends=True)
    start = lines.index("sources:\n")
    indent = re.match(r"\s*", lines[start + 1]).group()
    lines.insert(start + 1, f"{indent}- {{id: {CITE}, resource: /sources/{NAME}}}\n")
    claim = f"On 2026-09-20 the T arm led on registrations and on Study creation.[^{CITE}]"
    text = "".join(lines).replace("# Summary\n\n", f"# Summary\n\n{claim}\n\n", 1)
    text = re.sub(r"^status: \w+\n", "status: draft\n", text, count=1, flags=re.M)
    return re.sub(r"^generated:\n(?:  .*\n)+", "generated:\n  by: 'process:ai-wiki-curator'\n"
                  "  at: '2026-09-20T12:00:00Z'\n", text, count=1, flags=re.M)


def _history(repo: Path, claim: str = "The T arm registered more users.", *, new: str = NEW,
             retitle: bool = True) -> Path:
    """A bundle repository whose newest ingest the Codex path committed, followed by its audit."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    shutil.copytree(LIVE, repo, dirs_exist_ok=True)
    (repo / "index.md").write_text('---\nokf_version: "0.2"\n---\n# Bundle\n', encoding="utf-8")
    (repo / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")
    for path in sorted(repo.rglob("*.md")):
        for resource in re.findall(r"resource: (/sources/\S+)", path.read_text(encoding="utf-8")):
            stub = repo / resource.lstrip("/")
            stub.parent.mkdir(parents=True, exist_ok=True)
            stub.write_bytes(f"frozen evidence {stub.name}\n".encode())
    generate_indexes(repo)
    assert scan_sources.main([str(repo), "--commit"]) == 0
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "audit: ingest 000000000000")

    (repo / "sources" / NAME).write_bytes(EVIDENCE)
    for rel in (RETITLED, SPILLED):
        (repo / rel).write_text(_codex_edit((repo / rel).read_text(encoding="utf-8")), encoding="utf-8")
    if retitle:
        retitled = repo / RETITLED
        retitled.write_text(retitled.read_text(encoding="utf-8").replace(
            "title: Redacted title\n", "title: AI Study deferred login (2026-09)\n", 1), encoding="utf-8")
    (repo / new).write_text(
        "---\ntype: Metric\ntitle: Deferred login readout (2026-09-20)\n"
        "description: Registrations by arm in the deferred login readout of 2026-09-20.\n"
        "tags: [metric, login]\nstatus: draft\ngenerated:\n  by: 'process:ai-wiki-curator'\n"
        f"  at: '2026-09-20T12:00:00Z'\nsources:\n  - id: {CITE}\n    resource: /sources/{NAME}\n---\n"
        f"# Summary\n\n{claim}[^{CITE}]\n", encoding="utf-8")
    generate_indexes(repo)
    append_log.append(repo.resolve(), "ingest", f"Curated sources/inbox/{NAME}", [new, RETITLED, SPILLED],
                      day="2026-09-20")
    assert scan_sources.main([str(repo), "--commit"]) == 0
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"ingest: sources/inbox/{NAME}")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "audit: ingest 111111111111")
    return repo


@pytest.fixture(autouse=True)
def _git_on(monkeypatch):
    monkeypatch.delenv("AIWIKI_GIT", raising=False)


def _replay(repo: Path, capsys, *args: str) -> tuple[int, dict]:
    capsys.readouterr()  # closeout reports while the history is built
    code = replay.main(["--repo", str(repo), "--count", "1", *args])
    return code, json.loads(capsys.readouterr().out)


def _counts(report: dict) -> tuple:
    return tuple(report[key] for key in ("replayed", "accepted", "accepted_verbatim", "allow_declared",
                                         "equivalent", "passed"))


def test_a_codex_ingest_replays_as_a_changeset_that_differs_only_in_service_owned_bytes(tmp_path, capsys):
    history = _history(tmp_path / "bundle")
    refs = _git(history, "show-ref")
    ingest = _git(history, "rev-parse", "HEAD~1")

    code, report = _replay(history, capsys, "--declare-allow")

    assert code == 0
    assert report["declare_allow"] is True
    # Accepted, but only because the replay declared the retitle, as the gate requires.
    assert _counts(report) == (1, 1, 0, 1, 1, True)
    result = report["ingests"][0]
    assert (result["commit"], result["parent"]) == (ingest, _git(history, "rev-parse", "HEAD~2"))
    # The evidence id reproduces the snapshot path the committed concepts cite.
    assert (result["evidence_id"], result["source"]) == ("evidence", f"sources/{NAME}")
    assert (result["allow"], result["allow_declared"]) == ({"retype": [RETITLED]}, True)
    assert result["diff"] == {
        "concepts": {
            NEW: ["generated"],
            RETITLED: ["generated", "status"],  # the changeset keeps HEAD's stable
            SPILLED: ["generated", "spill"],  # the gate strips the spilled verification event
        },
        "closeout": ["log.md"],
        "other": [],
    }
    # --repo is read, never written: no ref moved and its tree is clean.
    assert _git(history, "show-ref") == refs
    assert _git(history, "status", "--porcelain") == ""


def test_an_undeclared_retitle_fails_a_verbatim_replay(tmp_path, capsys):
    history = _history(tmp_path / "bundle")

    code, report = _replay(history, capsys)

    assert code == 1
    assert report["declare_allow"] is False
    assert _counts(report) == (1, 0, 0, 0, 0, False)
    rejected = report["ingests"][0]
    assert (rejected["status"], rejected["http_status"]) == ("rejected", 422)
    assert {(error["code"], error.get("path")) for error in rejected["errors"]} == {("identity_locked", RETITLED)}
    # The report names the declaration the ingest would need.
    assert (rejected["allow"], rejected["allow_declared"]) == ({"retype": [RETITLED]}, False)


def test_a_non_ascii_concept_path_replays_verbatim(tmp_path, capsys):
    new = "metrics/延迟登录.md"
    history = _history(tmp_path / "bundle", new=new, retitle=False)

    code, report = _replay(history, capsys)

    assert code == 0
    assert _counts(report) == (1, 1, 1, 0, 1, True)
    result = report["ingests"][0]
    assert "allow" not in result
    assert result["files"] == 3
    assert result["diff"]["concepts"][new] == ["generated"]
    assert result["diff"]["other"] == []


def test_a_gate_rejection_fails_the_replay(tmp_path, capsys):
    # A credential the Codex path committed; the changeset gate refuses it.
    history = _history(tmp_path / "bundle", claim="Key AKIA" + "Z" * 16 + ".", retitle=False)

    code, report = _replay(history, capsys)

    assert code == 1
    assert _counts(report) == (1, 0, 0, 0, 0, False)
    rejected = report["ingests"][0]
    assert (rejected["status"], rejected["accepted"], rejected["equivalent"]) == ("rejected", False, False)
    assert {error["code"] for error in rejected["errors"]} == {"secret_detected"}


def _skip_indexes(monkeypatch):
    monkeypatch.setattr(curate, "generate_indexes", lambda _root: ([], []))


def _agent_owns_status(monkeypatch):
    monkeypatch.setitem(bookkeeping.SERVICE_KEYS, "changeset", ("verified", "generated"))


@pytest.mark.parametrize(("regress", "path", "found"), [
    # The writer stops regenerating indexes: the new concept is missing from its index.
    (_skip_indexes, "metrics/index.md", "other"),
    # The gate stops owning status: the stable concept is demoted to the Codex path's draft,
    # which matches history, so only the §2.4 value check sees it.
    (_agent_owns_status, RETITLED, "status_value"),
])
def test_a_writer_regression_fails_the_replay(tmp_path, capsys, monkeypatch, regress, path, found):
    history = _history(tmp_path / "bundle")
    regress(monkeypatch)

    code, report = _replay(history, capsys, "--declare-allow")

    assert code == 1
    assert _counts(report) == (1, 1, 0, 1, 0, False)
    diff = report["ingests"][0]["diff"]
    if found == "other":
        assert path in diff["other"]
    else:
        assert found in diff["concepts"][path]


HEAD = ("---\ntype: Metric\ntitle: T\ndescription: D\ntags: [a, b]\nstatus: {status}\n"
        "generated:\n  by: {by}\n  at: '2026-09-20T12:00:00Z'\nsources:\n  - id: s\n    resource: {resource}\n"
        "---\n{spill}# Summary\n\n{body}\n")


def _doc(status="draft", by="process:ai-wiki-curator", resource="/sources/a.md.source", spill="", body="Claim."):
    return HEAD.format(status=status, by=by, resource=resource, spill=spill, body=body)


@pytest.mark.parametrize(("replayed", "diff"), [
    (_doc(), []),
    (_doc(status="stable", by="process:ai-wiki-maintainer"), ["generated", "status"]),
    (_doc(resource="/sources/b.md.source"), ["sources[].resource"]),
    (_doc(spill="  - {by: process:ai-wiki-adversarial-audit, at: 2026-09-19T20:56:59Z}\n"), ["spill"]),
    (_doc(body="Another claim."), ["body"]),
    (_doc().replace("title: T", "title: U"), ["title"]),
    (_doc().replace("tags: [a, b]", "tags:\n  - a\n  - b"), ["format"]),
    (None, ["missing"]),
])
def test_concept_diff_names_every_difference(replayed, diff):
    assert replay.concept_diff(_doc(), replayed) == diff


ACTOR = "process:ai-wiki-maintainer"
STAMPED = f"{{by: '{ACTOR}', at: '2026-09-24T00:00:00Z'}}"
VERIFIED = "verified:\n  - {by: process:ai-wiki-adversarial-audit, at: '2026-09-21T00:00:00Z'}\n"


def _concept(status="stable", generated=STAMPED, verified=""):
    return (f"---\ntype: Metric\ntitle: T\ndescription: D\ntags: [a, b]\nstatus: {status}\n"
            f"generated: {generated}\n{verified}---\n# Summary\n\nClaim.\n")


PARENT = _concept(generated="{by: 'process:ai-wiki-curator', at: '2026-09-20T12:00:00Z'}", verified=VERIFIED)


@pytest.mark.parametrize(("parent", "replayed", "wrong"), [
    (PARENT, _concept(verified=VERIFIED), []),  # status kept, verified restored, stamped by the actor
    (PARENT, PARENT, []),  # a no-op restores generated
    (None, _concept(status="draft"), []),  # a new concept is draft and unverified
    (None, _concept(), ["status_value"]),
    (PARENT, _concept(status="draft", verified=VERIFIED), ["status_value"]),
    (PARENT, _concept(), ["verified_value"]),
    (None, _concept(status="draft", verified=VERIFIED), ["verified_value"]),
    (PARENT, _concept(generated="{by: 'human:x', at: '2026-09-24T00:00:00Z'}", verified=VERIFIED),
     ["generated_value"]),
])
def test_service_values_hold_the_replay_to_the_section_2_4_rules(parent, replayed, wrong):
    assert replay.service_values(parent, replayed, ACTOR) == wrong
