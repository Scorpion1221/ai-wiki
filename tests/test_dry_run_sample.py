"""scripts/dry_run_sample.py: the Phase 2 runbook's production check that the changeset gate
answers a sample of real concepts without a 5xx, sending each dry-run exactly once."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from gate_fixture import METRIC, Gate

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dry_run_sample", ROOT / "scripts" / "dry_run_sample.py")
sample = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sample)


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch)
    yield gate
    gate.close()


def _run(gate: Gate, capsys, *args: str) -> tuple[int, dict, list[tuple]]:
    calls = gate.connect("owner")  # a human: only a human uploads the probe packet
    code = sample.main(["-b", "kb-a", *args])
    return code, json.loads(capsys.readouterr().out), [call for call in calls if call[:2] == ("POST", "/changesets")]


def test_each_sampled_concept_is_judged_once_and_nothing_is_written(gate, capsys) -> None:
    head = gate.head()

    code, summary, posts = _run(gate, capsys, "--count", "3")

    assert code == 0, summary
    assert (summary["sent"], summary["skipped"], summary["server_errors"]) == (3, 0, []) and len(posts) == 3
    assert summary["base_revision"] == head and summary["http"] == {"200": 3}
    assert {row["status"] for row in summary["rows"]} == {"would_apply"}
    gate.assert_untouched(head)
    assert not (gate.writer / ".okf" / "jobs").exists()  # a dry-run creates no job


def test_a_5xx_is_counted_once_and_fails_the_check(gate, capsys, monkeypatch) -> None:
    judge = gate.appmod._dry_run

    def flaky(path, request, actor, evidence):
        if any(entry["path"] == METRIC for entry in request["files"]):
            return JSONResponse(status_code=503, content={"detail": "bundle mutation in progress; retry"})
        return judge(path, request, actor, evidence)

    monkeypatch.setattr(gate.appmod, "_dry_run", flaky)

    code, summary, posts = _run(gate, capsys, "--count", "6")

    assert code == 1
    assert [row["path"] for row in summary["server_errors"]] == [METRIC]  # never resent, unlike propose
    assert len(posts) == summary["sent"] == 6 and summary["http"]["503"] == 1


def test_a_concept_without_a_block_sources_list_is_skipped() -> None:
    assert sample.cite("---\ntype: Metric\ntitle: X\nsources: []\n---\n# X\n") is None
    cited = sample.cite("---\ntype: Metric\nsources:\n  - {id: a, resource: /sources/a.md.source}\n---\n# X\n")
    assert "  - {id: dry-run-probe, resource: evidence:packet}\n---\n" in cited
    assert cited.endswith("[^dry-run-probe]\n\n[^dry-run-probe]: dry-run probe\n")
