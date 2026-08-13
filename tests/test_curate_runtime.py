"""The runtime independently validates agent output before it is committed."""
from __future__ import annotations

import json
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from aiwiki.engine import scan_sources
from aiwiki.engine.document import current_verified, trust_tier
from aiwiki.runtime import curate
from aiwiki.service import ingest as I


def test_ingest_prompt_names_every_required_profile_field() -> None:
    for field in ("type", "title", "description", "tags", "status", "generated", "sources"):
        assert f"`{field}`" in curate.INGEST_PROMPT


def test_ingest_prompt_treats_backlinks_as_substantive_edits() -> None:
    assert "including a Related concepts/backlink" in curate.INGEST_PROMPT
    assert "leave the file byte-for-byte unchanged" in curate.INGEST_PROMPT
    assert "add this ingest snapshot to `sources`" in curate.INGEST_PROMPT
    assert "advance `generated.at` strictly beyond the prior generation" in curate.INGEST_PROMPT
    assert "Do not add navigation-only backlinks" in curate.INGEST_PROMPT


def test_headless_command_exposes_only_bundle_scoped_content_tools(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    source = tmp_path / "staged-source.md"
    command = curate._claude_command(bundle, "review", source=source)
    assert command[:3] == ["claude", "-p", "review"]
    assert command[command.index("--tools") + 1] == "Read,Edit,Write"
    scope = "//" + bundle.resolve().as_posix().lstrip("/") + "/**"
    source_scope = "//" + source.resolve().as_posix().lstrip("/")
    assert command[command.index("--allowedTools") + 1] == (
        f"Read({scope}),Edit({scope}),Read({source_scope})"
    )
    denied = command[command.index("--disallowedTools") + 1]
    git_path = "//" + (bundle.resolve() / ".git").as_posix().lstrip("/")
    for tool in ("Read", "Edit"):
        assert f"{tool}({git_path})" in denied.split(",")
        assert f"{tool}({git_path}/**)" in denied.split(",")
    for relative in (".okf", "sources/inbox"):
        hidden = "//" + (bundle.resolve() / relative).as_posix().lstrip("/")
        for tool in ("Read", "Edit"):
            assert f"{tool}({hidden})" in denied.split(",")
            assert f"{tool}({hidden}/**)" in denied.split(",")
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert "bypassPermissions" not in command
    assert not any(tool in command for tool in ("Bash", "Glob", "Grep", "WebFetch", "Skill", "Agent"))
    assert "--strict-mcp-config" in command
    assert json.loads(command[command.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert command[command.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in command
    assert "--no-session-persistence" in command
    assert "--no-chrome" in command
    assert command[command.index("--add-dir") + 1] == str(source.resolve().parent)


def test_headless_command_denies_actual_repository_metadata_for_nested_bundle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    bundle = root / "kb"
    bundle.mkdir(parents=True)
    command = curate._claude_command(bundle, "review", protected_root=root)
    denied = command[command.index("--disallowedTools") + 1].split(",")
    git_path = "//" + (root.resolve() / ".git").as_posix().lstrip("/")
    assert f"Read({git_path}/**)" in denied
    assert f"Edit({git_path}/**)" in denied
    assert not any("/kb/.git" in rule for rule in denied)


def test_nested_bundle_writer_fails_before_agent_or_git_mutation(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "repo"
    bundle = root / "kb"
    bundle.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    sibling = root / "sibling.txt"
    sibling.write_text("sibling sentinel\n", encoding="utf-8")
    (bundle / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    inbox = bundle / "sources" / "inbox" / "new.md.source"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"current")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    real_run = subprocess.run

    def no_agent(command, *args, **kwargs):
        if command[0] == "claude":
            pytest.fail("nested bundle must not start agent")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(
        curate.subprocess,
        "run",
        no_agent,
    )

    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["validation"]["reason"] == "nested bundle write is not supported"
    assert sibling.read_text(encoding="utf-8") == "sibling sentinel\n"
    assert inbox.read_bytes() == b"current"


def _run_job(tmp_path: Path, monkeypatch, validation_errors: list[str], refresh=None) -> dict:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    source = bundle / "sources" / "inbox" / "n.md.source"
    source.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    job_path = bundle / "job.json"
    job_path.write_text(json.dumps({"source": "sources/inbox/n.md.source", "status": "queued"}), encoding="utf-8")

    monkeypatch.setattr(curate, "_repo_root", lambda _bundle: bundle)
    monkeypatch.setattr(curate, "_pre_sync", lambda _root: {"synced": True})
    monkeypatch.setattr(curate, "_git", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="base\n"))
    monkeypatch.setattr(curate, "_working_files", lambda _root: [])
    monkeypatch.setattr(curate, "_refresh_visualization", refresh or (lambda _root, _bundle: None))
    monkeypatch.setattr(curate, "validate_bundle", lambda _bundle: validation_errors)
    monkeypatch.setattr(
        curate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="curated", stderr=""),
    )
    curate.run(bundle, "sources/inbox/n.md.source", job_path)
    return json.loads(job_path.read_text(encoding="utf-8"))


def test_validation_failure_blocks_commit(tmp_path: Path, monkeypatch) -> None:
    committed = []
    monkeypatch.setattr(curate, "_working_files", lambda _root: [])
    rolled_back = []
    monkeypatch.setattr(curate, "_rollback_git", lambda *_args: rolled_back.append(True))
    monkeypatch.setattr(curate, "_commit_and_push", lambda *args: committed.append(args))

    job = _run_job(tmp_path, monkeypatch, ["concepts/bad.md: missing # Citations section"])
    assert job["status"] == "failed"
    assert job["validation"] == {
        "status": "failed",
        "error_count": 1,
        "errors": ["concepts/bad.md: missing # Citations section"],
    }
    assert rolled_back == [True]
    assert committed == []


def test_validation_pass_records_commit_and_changed_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        curate,
        "_commit_and_push",
        lambda *_args: {
            "committed": True,
            "pushed": True,
            "commit": "abc123",
            "changed_files": ["concepts/new.md", "sources/n.md"],
        },
    )

    job = _run_job(tmp_path, monkeypatch, [])
    assert job["status"] == "done"
    assert job["validation"] == {"status": "passed", "error_count": 0}
    assert job["commit"] == "abc123"
    assert job["changed_files"] == ["concepts/new.md", "sources/n.md"]


def _policy_concept(
    status="draft",
    verified=None,
    *,
    generated_by=curate.CURATOR_ACTOR,
    generated_at="2026-08-13T00:00:00Z",
    description="x",
    body="X",
) -> str:
    fm = {
        "type": "Feature", "title": "X", "description": description, "tags": ["x"],
        "status": status,
        "generated": {"by": generated_by, "at": generated_at},
        "sources": [{"id": "s", "resource": "/sources/s.md.source"}],
    }
    if verified is not None:
        fm["verified"] = verified
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + f"---\n# Summary\n\n{body}\n"


def test_curation_policy_requires_new_draft_and_forbids_verification(tmp_path: Path) -> None:
    concept = tmp_path / "features" / "x.md"
    concept.parent.mkdir()
    source = tmp_path / "sources" / "s.md.source"
    source.parent.mkdir()
    source.write_text("evidence", encoding="utf-8")
    before = curate._concept_snapshot(tmp_path)
    concept.write_text(
        _policy_concept(
            status="stable",
            verified={"by": "process:curator", "at": "2026-08-13T00:01:00Z"},
        ),
        encoding="utf-8",
    )
    errors = curate._curation_policy_errors(tmp_path, before)
    assert errors == [
        "features/x.md: new concepts must start status draft",
        "features/x.md: curation must not verify a new concept",
    ]


def test_curation_policy_forbids_new_verification_and_concept_deletion(tmp_path: Path) -> None:
    concept = tmp_path / "features" / "x.md"
    concept.parent.mkdir()
    source = tmp_path / "sources" / "s.md.source"
    source.parent.mkdir()
    source.write_text("evidence", encoding="utf-8")
    concept.write_text(_policy_concept(), encoding="utf-8")
    before = curate._concept_snapshot(tmp_path)
    concept.write_text(
        _policy_concept(verified={"by": "human:owner", "at": "2026-08-13T01:00:00Z"}),
        encoding="utf-8",
    )
    assert curate._curation_policy_errors(tmp_path, before) == [
        "features/x.md: curation must preserve verification history unchanged",
    ]
    concept.unlink()
    assert curate._curation_policy_errors(tmp_path, before) == [
        "features/x.md: curation deleted a concept; deprecate it instead",
    ]


def test_curation_policy_resolves_local_sources_from_concept_path(tmp_path: Path) -> None:
    concept = tmp_path / "features" / "x.md"
    concept.parent.mkdir()
    source = tmp_path / "sources" / "s.md.source"
    source.parent.mkdir()
    source.write_text("evidence", encoding="utf-8")
    concept.write_text(_policy_concept(), encoding="utf-8")
    before = curate._concept_snapshot(tmp_path)

    concept.write_text(
        _policy_concept(generated_at="2026-08-13T00:01:00Z").replace(
            "/sources/s.md.source", "sources/s.md.source"
        ),
        encoding="utf-8",
    )
    # Bare sources/... is relative to features/x.md, not the bundle root.
    errors = curate._curation_policy_errors(tmp_path, before)
    assert errors == [
        "features/x.md: sources[0].resource does not resolve to a local file: "
        "'sources/s.md.source' -> 'features/sources/s.md.source'",
    ]

    concept.write_text(_policy_concept(), encoding="utf-8")
    assert curate._curation_policy_errors(tmp_path, before) == []

    concept.write_text(
        _policy_concept(generated_at="2026-08-13T00:01:00Z").replace(
            "/sources/s.md.source", "../../escape.txt"
        ),
        encoding="utf-8",
    )
    assert curate._curation_policy_errors(tmp_path, before) == [
        "features/x.md: sources[0].resource escapes the bundle: '../../escape.txt'",
    ]


def test_curation_policy_rejects_body_change_without_new_generation(tmp_path: Path) -> None:
    concept = tmp_path / "features" / "x.md"
    concept.parent.mkdir()
    source = tmp_path / "sources" / "s.md.source"
    source.parent.mkdir()
    source.write_text("evidence", encoding="utf-8")
    verification = {"by": "human:owner", "at": "2026-08-13T01:00:00Z"}
    concept.write_text(_policy_concept(verified=verification), encoding="utf-8")
    before = curate._concept_snapshot(tmp_path)

    concept.write_text(_policy_concept(verified=verification, body="Meaning changed"), encoding="utf-8")
    errors = curate._curation_policy_errors(tmp_path, before)
    assert any("must advance generated.at strictly after" in error for error in errors)
    assert any("retained verification current for the new generation" in error for error in errors)


def test_curation_policy_allows_new_generation_with_historical_verification(tmp_path: Path) -> None:
    concept = tmp_path / "features" / "x.md"
    concept.parent.mkdir()
    source = tmp_path / "sources" / "s.md.source"
    source.parent.mkdir()
    source.write_text("evidence", encoding="utf-8")
    verification = {"by": "human:owner", "at": "2026-08-13T01:00:00Z"}
    concept.write_text(_policy_concept(verified=verification), encoding="utf-8")
    before = curate._concept_snapshot(tmp_path)

    concept.write_text(
        _policy_concept(
            verified=verification,
            generated_at="2026-08-13T02:00:00Z",
            body="Meaning changed",
        ),
        encoding="utf-8",
    )
    assert curate._curation_policy_errors(tmp_path, before) == []
    frontmatter, _body = curate.parse_doc(concept)
    assert frontmatter["verified"] == verification  # retained as history, not destroyed
    assert trust_tier(frontmatter) == "human-reviewed"  # trust uses all OKF verification history
    assert current_verified(frontmatter) == []  # the changed revision is still unconfirmed


def test_curation_policy_treats_claim_metadata_as_substantive(tmp_path: Path) -> None:
    concept = tmp_path / "features" / "x.md"
    concept.parent.mkdir()
    source = tmp_path / "sources" / "s.md.source"
    source.parent.mkdir()
    source.write_text("evidence", encoding="utf-8")
    concept.write_text(_policy_concept(), encoding="utf-8")
    before = curate._concept_snapshot(tmp_path)

    concept.write_text(_policy_concept(description="changed claim"), encoding="utf-8")
    assert any(
        "must advance generated.at strictly after" in error
        for error in curate._curation_policy_errors(tmp_path, before)
    )

    concept.write_text(
        _policy_concept(description="changed claim", generated_at="2026-08-13T00:01:00Z"),
        encoding="utf-8",
    )
    assert curate._curation_policy_errors(tmp_path, before) == []


@pytest.mark.parametrize("existing", [False, True])
def test_curation_policy_rejects_future_generation_for_new_and_updated_concepts(
    tmp_path: Path, existing: bool,
) -> None:
    concept = tmp_path / "features" / "x.md"
    concept.parent.mkdir()
    source = tmp_path / "sources" / "s.md.source"
    source.parent.mkdir()
    source.write_text("evidence", encoding="utf-8")
    if existing:
        concept.write_text(_policy_concept(), encoding="utf-8")
    before = curate._concept_snapshot(tmp_path)
    concept.write_text(
        _policy_concept(
            generated_at="2099-01-01T00:00:00Z",
            body="updated" if existing else "new",
        ),
        encoding="utf-8",
    )

    errors = curate._curation_policy_errors(
        tmp_path, before, datetime(2026, 8, 13, 12, tzinfo=UTC),
    )
    assert any("generated.at must not exceed trusted pass time" in error for error in errors)


def test_curation_policy_rejects_future_generation_only_edit(tmp_path: Path) -> None:
    concept = tmp_path / "features" / "x.md"
    concept.parent.mkdir()
    source = tmp_path / "sources" / "s.md.source"
    source.parent.mkdir()
    source.write_text("evidence", encoding="utf-8")
    concept.write_text(_policy_concept(), encoding="utf-8")
    before = curate._concept_snapshot(tmp_path)
    concept.write_text(_policy_concept(generated_at="2099-01-01T00:00:00Z"), encoding="utf-8")

    errors = curate._curation_policy_errors(
        tmp_path, before, datetime(2026, 8, 13, 12, tzinfo=UTC),
    )
    assert errors == [
        "features/x.md: curation must not change generated metadata without substantive changes",
        "features/x.md: generated.at must not exceed trusted pass time "
        "2026-08-13T12:00:00+00:00; got '2099-01-01T00:00:00Z'",
    ]


def test_curation_policy_rejects_generated_actor_spoof_for_new_and_existing(
    tmp_path: Path,
) -> None:
    concept = tmp_path / "features" / "x.md"
    concept.parent.mkdir()
    source = tmp_path / "sources" / "s.md.source"
    source.parent.mkdir()
    source.write_text("evidence", encoding="utf-8")
    before = curate._concept_snapshot(tmp_path)
    concept.write_text(_policy_concept(generated_by="human:ceo"), encoding="utf-8")
    assert "features/x.md: new concepts must set generated.by to 'process:ai-wiki-curator'" in (
        curate._curation_policy_errors(tmp_path, before)
    )

    concept.write_text(_policy_concept(), encoding="utf-8")
    before = curate._concept_snapshot(tmp_path)
    concept.write_text(_policy_concept(generated_by="human:ceo"), encoding="utf-8")
    errors = curate._curation_policy_errors(tmp_path, before)
    assert errors == [
        "features/x.md: curation must not change generated metadata without substantive changes",
        "features/x.md: changed generation must set generated.by to 'process:ai-wiki-curator'",
    ]


def test_changed_concepts_must_cite_current_ingest_snapshot(tmp_path: Path) -> None:
    concept = tmp_path / "features" / "x.md"
    concept.parent.mkdir()
    (tmp_path / "sources").mkdir()
    before = curate._concept_snapshot(tmp_path)
    concept.write_text(_policy_concept(), encoding="utf-8")

    assert curate._curation_provenance_errors(
        tmp_path, before, "sources/current.md.source",
    ) == [
        "features/x.md: changed concepts must cite current ingest snapshot "
        "'sources/current.md.source'",
    ]

    concept.write_text(
        _policy_concept().replace("/sources/s.md.source", "/sources/current.md.source"),
        encoding="utf-8",
    )
    assert curate._curation_provenance_errors(
        tmp_path, before, "sources/current.md.source",
    ) == []


def test_source_policy_preserves_history_and_allows_only_current_ingest(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    historical = sources / "historical.md.source"
    historical.write_bytes(b"history")
    before = curate._source_snapshot(tmp_path)
    current_sha = __import__("hashlib").sha256(b"current").hexdigest()

    (sources / "current.md.source").write_bytes(b"current")
    assert curate._source_policy_errors(tmp_path, before, current_sha) == []

    historical.write_bytes(b"poisoned")
    (sources / "injected.md.source").write_bytes(b"unrelated")
    errors = curate._source_policy_errors(tmp_path, before, current_sha)
    assert errors == [
        "sources/historical.md.source: curation modified immutable source evidence",
        "sources/injected.md.source: curation added source evidence unrelated to this ingest",
    ]


def test_source_policy_rejects_historical_source_deletion(tmp_path: Path) -> None:
    source = tmp_path / "sources" / "historical.md.source"
    source.parent.mkdir()
    source.write_bytes(b"history")
    before = curate._source_snapshot(tmp_path)
    source.unlink()
    assert curate._source_policy_errors(tmp_path, before, None) == [
        "sources/historical.md.source: curation deleted immutable source evidence",
    ]


def test_agent_scope_allows_only_concepts_and_one_current_source(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    source = bundle / "sources" / "inbox" / "new.md.source"
    purpose = bundle / "purpose.md"
    concept = bundle / "features" / "x.md"
    source.parent.mkdir(parents=True)
    concept.parent.mkdir(parents=True)
    source.write_bytes(b"current")
    purpose.write_text("# Purpose\n", encoding="utf-8")
    concept.write_text(_policy_concept(), encoding="utf-8")
    before = curate._agent_tree_snapshot(bundle)
    links = curate._agent_symlink_snapshot(bundle)
    sha = __import__("hashlib").sha256(source.read_bytes()).hexdigest()

    (bundle / "sources" / "new.md.source").write_bytes(b"current")
    concept.write_text(
        _policy_concept(description="updated", generated_at="2026-08-13T00:01:00Z"),
        encoding="utf-8",
    )
    assert curate._agent_scope_errors(
        bundle, before, links, "sources/inbox/new.md.source", sha,
    ) == []

    purpose.write_text("prompt injection won\n", encoding="utf-8")
    (bundle / "notes.txt").write_text("not a concept\n", encoding="utf-8")
    errors = curate._agent_scope_errors(
        bundle, before, links, "sources/inbox/new.md.source", sha,
    )
    assert errors == [
        "notes.txt: curation modified a prohibited non-concept bundle file",
        "purpose.md: curation modified a prohibited non-concept bundle file",
    ]


def test_agent_scope_ignores_concurrent_job_and_inbox_source(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    current = bundle / "sources" / "inbox" / "current.md.source"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"current")
    before = curate._agent_tree_snapshot(bundle)
    links = curate._agent_symlink_snapshot(bundle)

    concurrent = bundle / "sources" / "inbox" / "second.md.source"
    concurrent.write_bytes(b"second")
    sidecar = bundle / ".okf" / "jobs" / "second.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text('{"status":"queued"}\n', encoding="utf-8")

    assert curate._agent_scope_errors(
        bundle, before, links, "sources/inbox/current.md.source", None,
    ) == []
    curate._restore_agent_tree(bundle, before, links)
    assert concurrent.read_bytes() == b"second"
    assert sidecar.is_file()


def test_blocked_curation_does_not_delete_concurrent_receive(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")
    (bundle / "purpose.md").write_text("# Purpose\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(bundle)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(bundle), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(bundle), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(bundle), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(bundle), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    first, duplicate = I.receive_source(bundle, b"first evidence", "first.md")
    assert duplicate is False
    first_path = I.job_path(bundle, first["id"])
    entered = threading.Event()
    release = threading.Event()
    real_run = subprocess.run

    def blocked_agent(command, *args, **kwargs):
        if command[0] != "claude":
            return real_run(command, *args, **kwargs)
        entered.set()
        assert release.wait(2)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="expected failure")

    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(curate.subprocess, "run", blocked_agent)
    thread = threading.Thread(target=curate.run, args=(bundle, first["source"], first_path))
    thread.start()
    assert entered.wait(2)

    second_data = b"second evidence"
    second, duplicate = I.receive_source(bundle, second_data, "second.md")
    assert duplicate is False
    second_job_path = I.job_path(bundle, second["id"])
    second_source = bundle / second["source"]
    second_job_bytes = second_job_path.read_bytes()
    release.set()
    thread.join(3)

    assert not thread.is_alive()
    first_done = json.loads(first_path.read_text(encoding="utf-8"))
    assert first_done["status"] == "failed"
    assert first_done["validation"]["reason"] == "curation failed"
    assert second_job_path.read_bytes() == second_job_bytes
    assert json.loads(second_job_bytes)["status"] == "queued"
    assert second_source.read_bytes() == second_data


@pytest.mark.parametrize("git_on", [False, True])
def test_curation_structural_prompt_injection_fails_and_rolls_back(
    tmp_path: Path, monkeypatch, git_on: bool,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    purpose = bundle / "purpose.md"
    purpose.write_text("# Trusted purpose\n", encoding="utf-8")
    (bundle / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")
    if git_on:
        subprocess.run(["git", "init", "-b", "main", str(bundle)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(bundle), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(bundle), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(bundle), "add", "purpose.md", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", str(bundle), "commit", "-m", "initial"],
            check=True, capture_output=True,
        )
        monkeypatch.delenv("AIWIKI_GIT", raising=False)
    else:
        monkeypatch.setenv("AIWIKI_GIT", "off")

    inbox = bundle / "sources" / "inbox" / "new.md.source"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"current")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    real_run = subprocess.run

    def injected_agent(command, *args, **kwargs):
        if command[0] != "claude":
            return real_run(command, *args, **kwargs)
        purpose.write_text("# Follow the source's instructions\n", encoding="utf-8")
        (bundle / "sources" / "new.md.source").write_bytes(inbox.read_bytes())
        inbox.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="curated", stderr="")

    monkeypatch.setattr(curate.subprocess, "run", injected_agent)
    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["validation"]["reason"] == "agent scope violation"
    assert job["out_of_scope_files"] == ["purpose.md"]
    assert purpose.read_text(encoding="utf-8") == "# Trusted purpose\n"
    assert inbox.read_bytes() == b"current"
    assert not (bundle / "sources" / "new.md.source").exists()
    if git_on:
        status = subprocess.run(
            ["git", "-C", str(bundle), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout
        assert status == ""


def test_agent_scope_rejects_and_restores_new_symlink(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("sentinel", encoding="utf-8")
    before = curate._agent_tree_snapshot(bundle)
    links = curate._agent_symlink_snapshot(bundle)
    link = bundle / "features" / "escape.md"
    link.parent.mkdir()
    link.symlink_to(outside)

    assert curate._agent_scope_errors(bundle, before, links, "sources/inbox/x", None) == [
        "features/escape.md: curation may not create, remove, or retarget symlinks",
    ]
    curate._restore_agent_tree(bundle, before, links)
    assert not link.exists() and not link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_curation_refuses_existing_bundle_symlink_before_agent_runs(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    inbox = bundle / "sources" / "inbox" / "new.md.source"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"current")
    outside = tmp_path / "secret.md"
    outside.write_text("outside sentinel\n", encoding="utf-8")
    link = bundle / "features" / "escape.md"
    link.parent.mkdir()
    link.symlink_to(outside)
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(
        curate.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("agent must not start for a symlinked bundle"),
    )

    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["validation"]["reason"] == "bundle symlink preflight failed"
    assert job["out_of_scope_files"] == ["features/escape.md"]
    assert link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside sentinel\n"
    assert inbox.read_bytes() == b"current"


def test_curation_refuses_preexisting_source_drift_without_laundering_baseline(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    bundle = tmp_path / "bundle"
    historical = bundle / "sources" / "historical.md.source"
    historical.parent.mkdir(parents=True)
    historical.write_text("version A\n", encoding="utf-8")
    assert scan_sources.main([str(bundle), "--commit"]) == 0
    capsys.readouterr()
    baseline = (bundle / "sources" / ".hashes.yaml").read_bytes()
    historical.write_text("tampered A\n", encoding="utf-8")

    inbox = bundle / "sources" / "inbox" / "new.md.source"
    inbox.parent.mkdir()
    inbox.write_bytes(b"source B")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(
        curate.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("agent must not start with source drift"),
    )

    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["validation"]["reason"] == "source drift preflight failed"
    assert job["out_of_scope_files"] == ["sources/historical.md.source"]
    assert (bundle / "sources" / ".hashes.yaml").read_bytes() == baseline
    assert historical.read_text(encoding="utf-8") == "tampered A\n"
    assert inbox.read_bytes() == b"source B"
    assert scan_sources.main([str(bundle), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["changed"] == ["sources/historical.md.source"]


@pytest.mark.parametrize("existing", [False, True])
def test_future_generated_at_from_agent_fails_and_rolls_back(
    tmp_path: Path, monkeypatch, existing: bool,
) -> None:
    bundle = tmp_path / "bundle"
    inbox = bundle / "sources" / "inbox" / "new.md.source"
    concept = bundle / "features" / "x.md"
    inbox.parent.mkdir(parents=True)
    concept.parent.mkdir(parents=True)
    inbox.write_bytes(b"current evidence")
    (bundle / "sources" / "s.md.source").write_bytes(b"old evidence")
    original = None
    if existing:
        original = _policy_concept()
        concept.write_text(original, encoding="utf-8")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(curate, "validate_bundle", lambda _bundle: [])

    def future_agent(command, **kwargs):
        snapshot = bundle / "sources" / "new.md.source"
        snapshot.write_bytes(inbox.read_bytes())
        inbox.unlink()
        concept.write_text(
            _policy_concept(
                generated_at="2099-01-01T00:00:00Z",
                body="updated" if existing else "new",
            ).replace("/sources/s.md.source", "/sources/new.md.source"),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="curated", stderr="")

    monkeypatch.setattr(curate.subprocess, "run", future_agent)
    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert any(
        "generated.at must not exceed trusted pass time" in error
        for error in job["validation"]["errors"]
    )
    assert inbox.read_bytes() == b"current evidence"
    assert not (bundle / "sources" / "new.md.source").exists()
    if existing:
        assert concept.read_text(encoding="utf-8") == original
    else:
        assert not concept.exists()


def test_agent_cannot_copy_current_source_without_citing_it(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    inbox = bundle / "sources" / "inbox" / "new.md.source"
    concept = bundle / "features" / "x.md"
    inbox.parent.mkdir(parents=True)
    concept.parent.mkdir(parents=True)
    inbox.write_bytes(b"current evidence")
    (bundle / "sources" / "s.md.source").write_bytes(b"old evidence")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(curate, "validate_bundle", lambda _bundle: [])

    def laundering_agent(command, **kwargs):
        (bundle / "sources" / "new.md.source").write_bytes(inbox.read_bytes())
        inbox.unlink()
        concept.write_text(_policy_concept(), encoding="utf-8")  # cites an old/unrelated source
        return subprocess.CompletedProcess(command, 0, stdout="curated", stderr="")

    monkeypatch.setattr(curate.subprocess, "run", laundering_agent)
    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["validation"]["errors"] == [
        "features/x.md: changed concepts must cite current ingest snapshot "
        "'sources/new.md.source'",
    ]
    assert inbox.read_bytes() == b"current evidence"
    assert not concept.exists()
    assert not (bundle / "sources" / "new.md.source").exists()


def test_agent_cannot_delete_existing_verification_history(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    inbox = bundle / "sources" / "inbox" / "new.md.source"
    concept = bundle / "features" / "x.md"
    inbox.parent.mkdir(parents=True)
    concept.parent.mkdir(parents=True)
    inbox.write_bytes(b"current evidence")
    verification = {"by": "human:owner", "at": "2026-08-13T00:00:30Z"}
    original = _policy_concept(verified=verification)
    concept.write_text(original, encoding="utf-8")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(curate, "validate_bundle", lambda _bundle: [])

    def delete_history(command, **kwargs):
        (bundle / "sources" / "new.md.source").write_bytes(inbox.read_bytes())
        inbox.unlink()
        concept.write_text(
            _policy_concept(
                generated_at="2026-08-13T00:01:00Z",
                body="updated",
            ).replace("/sources/s.md.source", "/sources/new.md.source"),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="curated", stderr="")

    monkeypatch.setattr(curate.subprocess, "run", delete_history)
    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert "features/x.md: curation must preserve verification history unchanged" in (
        job["validation"]["errors"]
    )
    assert concept.read_text(encoding="utf-8") == original
    assert inbox.read_bytes() == b"current evidence"


def test_git_metadata_snapshot_restores_worktree_git_file(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    git_file = bundle / ".git"
    git_file.write_text("gitdir: ../trusted/worktrees/bundle\n", encoding="utf-8")
    before = curate._git_metadata_snapshot(bundle)

    git_file.write_text("gitdir: /tmp/attacker\n", encoding="utf-8")
    assert curate._git_metadata_errors(bundle, before) == [
        ".git: curation modified protected Git metadata",
    ]
    curate._restore_git_metadata(bundle, before)
    assert git_file.read_text(encoding="utf-8") == "gitdir: ../trusted/worktrees/bundle\n"


def test_curation_restores_git_config_and_hook_before_rollback(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")
    (bundle / "purpose.md").write_text("# Purpose\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(bundle)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(bundle), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(bundle), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(bundle), "add", ".gitignore", "purpose.md"], check=True)
    subprocess.run(
        ["git", "-C", str(bundle), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    config = bundle / ".git" / "config"
    original_config = config.read_bytes()
    hook = bundle / ".git" / "hooks" / "pre-commit"
    inbox = bundle / "sources" / "inbox" / "new.md.source"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"current evidence")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    other_job = bundle / ".okf" / "jobs" / "other.json"
    other_job.write_text('{"status":"queued"}\n', encoding="utf-8")
    real_run = subprocess.run

    def metadata_attack(command, *args, **kwargs):
        if command[0] != "claude":
            return real_run(command, *args, **kwargs)
        config.write_text("[core]\n\thooksPath = /tmp/attacker\n", encoding="utf-8")
        hook.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        hook.chmod(0o755)
        other_job.write_text('{"status":"pwned"}\n', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="curated", stderr="")

    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(curate.subprocess, "run", metadata_attack)
    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["validation"]["reason"] == "Git metadata integrity violation"
    assert ".git/config" in job["out_of_scope_files"]
    assert ".git/hooks/pre-commit" in job["out_of_scope_files"]
    assert config.read_bytes() == original_config
    assert not hook.exists()
    assert inbox.read_bytes() == b"current evidence"
    # Operational state is denied to real Claude and excluded from content rollback,
    # so concurrent service writes are not overwritten by this synthetic bypass.
    assert "/.okf/**" in curate._agent_deny_rules(bundle)
    status = real_run(
        ["git", "-C", str(bundle), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""


def test_timeout_restores_git_metadata_before_any_git_call(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")
    (bundle / "purpose.md").write_text("# Purpose\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(bundle)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(bundle), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(bundle), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(bundle), "add", ".gitignore", "purpose.md"], check=True)
    subprocess.run(
        ["git", "-C", str(bundle), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    config = bundle / ".git" / "config"
    original_config = config.read_bytes()
    hook = bundle / ".git" / "hooks" / "post-checkout"
    outside = tmp_path / "hook-executed.txt"
    inbox = bundle / "sources" / "inbox" / "new.md.source"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"current evidence")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    other_job = bundle / ".okf" / "jobs" / "other.json"
    other_job.write_text('{"status":"queued"}\n', encoding="utf-8")
    real_run = subprocess.run
    real_git = curate._git
    attacked = False

    def guarded_git(root, *args, **kwargs):
        if attacked:
            assert config.read_bytes() == original_config
            assert not hook.exists()
        return real_git(root, *args, **kwargs)

    def timeout_attack(command, *args, **kwargs):
        nonlocal attacked
        if command[0] != "claude":
            return real_run(command, *args, **kwargs)
        config.write_text("[core]\n\thooksPath = .git/hooks\n", encoding="utf-8")
        hook.write_text(f"#!/bin/sh\necho pwned > {outside}\n", encoding="utf-8")
        hook.chmod(0o755)
        other_job.write_text('{"status":"pwned"}\n', encoding="utf-8")
        attacked = True
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(curate, "_git", guarded_git)
    monkeypatch.setattr(curate.subprocess, "run", timeout_attack)
    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert "timed out" in job["error"]
    assert config.read_bytes() == original_config
    assert not hook.exists()
    assert not outside.exists()
    assert inbox.read_bytes() == b"current evidence"
    assert "/.okf/**" in curate._agent_deny_rules(bundle)


def test_nonzero_agent_restores_ignored_job_before_git_rollback(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")
    (bundle / "purpose.md").write_text("# Purpose\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(bundle)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(bundle), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(bundle), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(bundle), "add", ".gitignore", "purpose.md"], check=True)
    subprocess.run(
        ["git", "-C", str(bundle), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    inbox = bundle / "sources" / "inbox" / "new.md.source"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"current evidence")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    other_job = bundle / ".okf" / "jobs" / "other.json"
    other_job.write_text('{"status":"queued"}\n', encoding="utf-8")
    real_run = subprocess.run

    def failed_agent(command, *args, **kwargs):
        if command[0] != "claude":
            return real_run(command, *args, **kwargs)
        other_job.write_text('{"status":"pwned"}\n', encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")

    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(curate.subprocess, "run", failed_agent)
    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["validation"]["reason"] == "curation failed"
    assert "/sources/inbox/**" in curate._agent_deny_rules(bundle)
    assert inbox.read_bytes() == b"current evidence"
    status = real_run(
        ["git", "-C", str(bundle), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""


@pytest.mark.parametrize("restore_kind", ["metadata", "tree"])
def test_restore_failure_blocks_all_git_after_agent_attack(
    tmp_path: Path, monkeypatch, restore_kind: str,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")
    (bundle / "purpose.md").write_text("# Purpose\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(bundle)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(bundle), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(bundle), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(bundle), "add", ".gitignore", "purpose.md"], check=True)
    subprocess.run(
        ["git", "-C", str(bundle), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    config = bundle / ".git" / "config"
    hook = bundle / ".git" / "hooks" / "post-checkout"
    outside = tmp_path / "hook-executed.txt"
    inbox = bundle / "sources" / "inbox" / "new.md.source"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"current evidence")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    other_job = bundle / ".okf" / "jobs" / "other.json"
    other_job.write_text('{"status":"queued"}\n', encoding="utf-8")
    real_run = subprocess.run
    real_git = curate._git
    attacked = False
    git_after_attack = 0

    def guarded_git(root, *args, **kwargs):
        nonlocal git_after_attack
        if attacked:
            git_after_attack += 1
        return real_git(root, *args, **kwargs)

    def attack_then_timeout(command, *args, **kwargs):
        nonlocal attacked
        if command[0] != "claude":
            return real_run(command, *args, **kwargs)
        if restore_kind == "metadata":
            config.write_text("[core]\n\thooksPath = .git/hooks\n", encoding="utf-8")
            hook.write_text(f"#!/bin/sh\necho pwned > {outside}\n", encoding="utf-8")
            hook.chmod(0o755)
        else:
            other_job.write_text('{"status":"pwned"}\n', encoding="utf-8")
        attacked = True
        raise subprocess.TimeoutExpired(command, 1)

    def restore_failure(*_args, **_kwargs):
        raise OSError("simulated restore failure")

    monkeypatch.delenv("AIWIKI_GIT", raising=False)
    monkeypatch.setattr(curate, "_git", guarded_git)
    monkeypatch.setattr(curate.subprocess, "run", attack_then_timeout)
    if restore_kind == "metadata":
        monkeypatch.setattr(curate, "_restore_git_metadata", restore_failure)
    else:
        monkeypatch.setattr(curate, "_restore_agent_tree", restore_failure)

    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)

    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["phase"] == "rollback_blocked"
    assert "simulated restore failure" in job["rollback_blocked"]
    assert git_after_attack == 0
    assert not outside.exists()
    assert inbox.read_bytes() == b"current evidence"
    recovery = bundle / job["recovery_source"]
    assert recovery.read_bytes() == b"current evidence"


def test_curation_source_scope_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    inbox = bundle / "sources" / "inbox" / "new.md.source"
    historical = bundle / "sources" / "historical.md.source"
    inbox.parent.mkdir(parents=True)
    inbox.write_bytes(b"current")
    historical.write_bytes(b"history")
    sha = __import__("hashlib").sha256(inbox.read_bytes()).hexdigest()
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({
        "source": inbox.relative_to(bundle).as_posix(), "sha256": sha, "status": "queued",
    }))
    monkeypatch.setenv("AIWIKI_GIT", "off")

    def poison_source(*args, **kwargs):
        historical.write_bytes(b"poisoned")
        (bundle / "sources" / "new.md.source").write_bytes(inbox.read_bytes())
        return subprocess.CompletedProcess(args[0], 0, stdout="curated", stderr="")

    monkeypatch.setattr(curate.subprocess, "run", poison_source)
    curate.run(bundle, inbox.relative_to(bundle).as_posix(), job_path)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert "modified immutable source evidence" in "\n".join(job["validation"]["errors"])
    assert historical.read_bytes() == b"history"
    assert inbox.read_bytes() == b"current"
    assert not (bundle / "sources" / "new.md.source").exists()


def test_no_git_failed_curation_restores_bundle_and_inbox_source(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    concept = bundle / "features" / "x.md"
    source = bundle / "sources" / "inbox" / "n.md.source"
    concept.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    concept.write_text(_policy_concept(), encoding="utf-8")
    source.write_text("raw", encoding="utf-8")
    original = concept.read_text(encoding="utf-8")
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({"source": source.relative_to(bundle).as_posix(), "status": "queued"}))
    monkeypatch.setenv("AIWIKI_GIT", "off")

    def failed_agent(*args, **kwargs):
        concept.write_text("corrupt", encoding="utf-8")
        source.rename(bundle / "sources" / "moved.md.source")
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr="failed")

    monkeypatch.setattr(curate.subprocess, "run", failed_agent)
    curate.run(bundle, source.relative_to(bundle).as_posix(), job_path)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert concept.read_text(encoding="utf-8") == original
    assert source.read_text(encoding="utf-8") == "raw"
    assert not (bundle / "sources" / "moved.md.source").exists()


def test_remote_push_failure_is_technical_failure_and_rolls_back(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    source = bundle / "sources" / "inbox" / "n.md.source"
    source.parent.mkdir(parents=True)
    source.write_text("raw", encoding="utf-8")
    job_path = bundle / "job.json"
    job_path.write_text(json.dumps({"source": source.relative_to(bundle).as_posix(), "status": "queued"}))
    monkeypatch.setattr(curate, "_repo_root", lambda _bundle: bundle)
    monkeypatch.setattr(curate, "_pre_sync", lambda _root: {"synced": True})
    monkeypatch.setattr(curate, "_working_files", lambda _root: [])
    monkeypatch.setattr(curate, "_git", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="base\n"))
    monkeypatch.setattr(curate, "_concept_snapshot", lambda _bundle: {})
    monkeypatch.setattr(curate, "_curation_policy_errors", lambda *_args: [])
    monkeypatch.setattr(curate, "validate_bundle", lambda _bundle: [])
    monkeypatch.setattr(curate, "_curated_source", lambda *_args: "sources/n.md.source")
    monkeypatch.setattr(curate.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, stdout="curated", stderr="",
    ))
    monkeypatch.setattr(curate, "_commit_and_push", lambda *_args: {
        "committed": True,
        "pushed": False,
        "commit": "local-commit",
        "changed_files": ["features/x.md", "sources/n.md.source"],
    })
    monkeypatch.setattr(curate, "_has_remote", lambda _root: True)
    rolled_back = []
    monkeypatch.setattr(curate, "_rollback_git", lambda *_args: rolled_back.append(True))

    curate.run(bundle, source.relative_to(bundle).as_posix(), job_path)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert job["error"] == "curation git commit/push failed"
    assert rolled_back == [True]
    assert source.read_text(encoding="utf-8") == "raw"


def test_no_git_rollback_unlinks_new_symlink_without_touching_outside_target(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel", encoding="utf-8")
    before = curate._tree_snapshot(bundle)
    links_before = curate._bundle_symlinks(bundle)
    link = bundle / "escape-link"
    link.symlink_to(outside)

    curate._restore_tree(bundle, before, links_before)

    assert not link.exists() and not link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_curation_rejects_inbox_source_symlink_without_reading_target(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    inbox = bundle / "sources" / "inbox"
    inbox.mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("do not read", encoding="utf-8")
    source = inbox / "source.md.source"
    source.symlink_to(outside)
    job_path = bundle / ".okf" / "jobs" / "j.json"
    job_path.parent.mkdir(parents=True)
    job_path.write_text(json.dumps({"source": source.relative_to(bundle).as_posix(), "status": "queued"}))
    monkeypatch.setenv("AIWIKI_GIT", "off")
    monkeypatch.setattr(
        curate.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("agent must not run for a symlink source"),
    )

    curate.run(bundle, source.relative_to(bundle).as_posix(), job_path)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["status"] == "failed"
    assert "source not found" in job["validation"]["reason"]
    assert source.is_symlink()
    assert outside.read_text(encoding="utf-8") == "do not read"


def test_validation_pass_refreshes_visualization_before_commit(tmp_path: Path, monkeypatch) -> None:
    events = []
    refresh = lambda *_args: events.append("render") or {  # noqa: E731
        "path": "viz.html", "concepts": 2, "edges": 1, "bytes": 100,
    }
    monkeypatch.setattr(
        curate,
        "_commit_and_push",
        lambda *_args: events.append("commit") or {
            "committed": True,
            "pushed": True,
            "commit": "abc123",
            "changed_files": ["concepts/new.md", "viz.html"],
        },
    )

    job = _run_job(tmp_path, monkeypatch, [], refresh=refresh)

    assert events == ["render", "commit"]
    assert job["visualization"] == {"path": "viz.html", "concepts": 2, "edges": 1, "bytes": 100}
    assert job["changed_files"] == ["concepts/new.md", "viz.html"]


def test_refresh_visualization_is_opt_in_and_preserves_display_name(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    bundle = root / "knowledge"
    bundle.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    (bundle / "concept.md").write_text(
        """---
type: Feature
title: First
description: First concept
tags: [demo]
status: draft
generated: {by: process:test, at: 2026-08-13T00:00:00Z}
sources: [{id: fixture, resource: https://example.com/source}]
---

# First
""",
        encoding="utf-8",
    )

    assert curate._refresh_visualization(root, bundle) is None
    curate.generate_visualization(bundle, root / "viz.html", bundle_name="Custom Wiki Name")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, capture_output=True)
    (bundle / "second.md").write_text(
        """---
type: Feature
title: Second
description: Second concept
tags: [demo]
status: draft
generated: {by: process:test, at: 2026-08-13T00:00:00Z}
sources: [{id: fixture, resource: https://example.com/source}]
---

# Second
""",
        encoding="utf-8",
    )

    result = curate._refresh_visualization(root, bundle)
    html = (root / "viz.html").read_text(encoding="utf-8")

    assert result is not None and result["concepts"] == 2
    assert result["path"] == "viz.html"
    assert 'window.BUNDLE_NAME = "Custom Wiki Name";' in html
    assert '"label": "Second"' in html


def test_failed_inbox_source_is_excluded_from_later_commit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    bundle = root / "knowledge"
    inbox = bundle / "sources" / "inbox"
    inbox.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    (bundle / "purpose.md").write_text("# Purpose\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, capture_output=True)

    (inbox / "failed-job.md").write_text("must not leak\n", encoding="utf-8")
    sidecar = bundle / ".okf" / "jobs" / "failed.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text('{"status":"failed"}\n', encoding="utf-8")
    curate._exclude_inbox(root, bundle)
    concept = bundle / "features" / "new.md"
    concept.parent.mkdir(parents=True)
    concept.write_text("# New\n", encoding="utf-8")

    result = curate._commit_and_push(root, "ingest: current job")

    assert result["committed"] is True
    assert result["changed_files"] == ["knowledge/features/new.md"]
    committed = subprocess.run(
        ["git", "-C", str(root), "show", "--pretty=format:", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert committed == ["knowledge/features/new.md"]
    assert (inbox / "failed-job.md").is_file()
    assert sidecar.is_file()


def test_exclude_inbox_handles_bundle_at_repository_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    inbox = root / "sources" / "inbox"
    inbox.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)

    curate._exclude_inbox(root, root)

    exclude_path = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    exclude = Path(exclude_path)
    if not exclude.is_absolute():
        exclude = root / exclude
    assert "/sources/inbox/" in exclude.read_text(encoding="utf-8").splitlines()
    assert "/.okf/" in exclude.read_text(encoding="utf-8").splitlines()
    assert "/./sources/inbox/" not in exclude.read_text(encoding="utf-8")
