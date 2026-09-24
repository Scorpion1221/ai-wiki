"""``ai-wiki admin`` (design §3, §8.5, §9; acceptance §13 W11): changesets, revert, compare and
cursor import, driven through the real CLI and its urllib client against the writer app.

``compare`` renders a golden side-by-side diff; the other verbs are checked end to end.
"""
from __future__ import annotations

import http.client
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest
from gate_fixture import METRIC, OPERATOR, TOKENS, Gate, cited, clone, concept, git

from aiwiki.cli import admin
from aiwiki.cli import main as cli

GOLDEN = Path(__file__).parent / "fixtures" / "admin" / "compare.golden"
BLIND_GOLDEN = GOLDEN.with_name("compare-blind.golden")


@pytest.fixture
def gate(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch)
    yield gate
    gate.close()


@pytest.fixture
def owner(gate, tmp_path, monkeypatch):
    """The CLI configured with the owner's token; urllib's requests are answered by the gate's app."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"endpoint": "http://writer/", "token": TOKENS["owner"], "bundle": "kb-a"}))
    monkeypatch.setattr(cli, "CONFIG", config)
    monkeypatch.delenv("AIWIKI_TOKEN", raising=False)
    monkeypatch.setattr(admin, "POLL_S", 0.05)
    gate.calls = []

    def urlopen(request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        assert parts.netloc == "writer" and timeout
        response = gate.client.request(request.get_method(), parts.path + (f"?{parts.query}" if parts.query else ""),
                                       content=request.data, headers=dict(request.header_items()))
        gate.calls.append((request.get_method(), parts.path, response.status_code))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, response.status_code, "", response.headers,
                                         io.BytesIO(response.content))
        answer = io.BytesIO(response.content)
        answer.status, answer.headers = response.status_code, response.headers
        return answer

    monkeypatch.setattr(admin.urllib.request, "urlopen", urlopen)
    return gate


def run(capsys, *args: str) -> tuple[int, str]:
    capsys.readouterr()
    try:
        code = cli.main(list(args))
    except SystemExit as exit_:
        code = exit_.code
    return code, capsys.readouterr().out


def document(out: str) -> dict:
    """The JSON the CLI printed; the in-process worker's closeout may print its report before it."""
    return json.loads(out[out.index("{\n"):])


def commit(gate: Gate, *files: dict, bundle: str = "kb-a") -> dict:
    response = gate.post(gate.request(*files, bundle=bundle), bundle=bundle)
    assert response.status_code == 201, response.text
    return response.json()


# --- compare -----------------------------------------------------------------------------------

FUNNEL = """---
type: Metric
title: Plugin funnel
description: Install to first payment
generated:
  by: {by}
  at: '{at}'
sources:
- id: waio-1
  resource: /sources/waio-1.md.source
{extra}---
# Summary

The plugin funnel counts installs that reach a first payment.
{claim}
# Definition

- numerator: first payments
- denominator: installs
- window: 7 days
- owner: growth
"""
BASE = {
    "index.md": b'---\nokf_version: "0.2"\n---\n# Bundle\n',
    "metrics/funnel.md": FUNNEL.format(by="process:ai-wiki-curator", at="2026-09-17T21:23:50Z", extra="",
                                       claim="").encode(),
    "metrics/stamped.md": b"---\ntype: Metric\ntitle: Stamped\n---\n# Summary\n\nUnchanged body.\n",
    "metrics/same.md": b"---\ntype: Metric\ntitle: Same\n---\n# Summary\n\nOld line.\n",
}
LIVE = {
    **BASE,
    "index.md": b'---\nokf_version: "0.2"\n---\n# Bundle\n\n* [New](metrics/new.md)\n',
    "metrics/funnel.md": FUNNEL.format(
        by="process:ai-wiki-curator", at="2026-10-17T04:12:09Z",
        extra="- id: waio-612\n  resource: /sources/waio-612.md.source\n",
        claim="\nThe 7-day rate rose to 4.1% after the checkout fix.[^waio-612]\n").encode(),
    "metrics/new.md": b"---\ntype: Metric\ntitle: New\n---\n# Summary\n\nA metric only the live flow created.\n",
    "metrics/same.md": b"---\ntype: Metric\ntitle: Same\ngenerated: {by: process:ai-wiki-curator}\n---\n"
                       b"# Summary\n\nNew line.\n",
    "sources/waio-612.md.source": b"live evidence\n",
}
SHADOW = {
    **BASE,
    "metrics/funnel.md": FUNNEL.format(
        by="process:ai-wiki-maintainer", at="2026-10-17T05:30:41Z",
        extra="- id: waio-612\n  resource: /sources/waio-612-3f2a.md.source\n",
        claim="\n结账修复后，7 日首付率从 3.2% 升到 4.1%，来源是 WAIO-612 的周报，覆盖 2026-10-09 至 2026-10-16 "
              "的全部插件安装。[^waio-612]\n").encode(),
    # An audit stamp alone is no content change.
    "metrics/stamped.md": b"---\ntype: Metric\ntitle: Stamped\nverified:\n- {by: process:ai-wiki-auditor, "
                          b"at: '2026-10-17T07:00:00Z'}\n---\n# Summary\n\nUnchanged body.\n",
    "metrics/same.md": b"---\ntype: Metric\ntitle: Same\ngenerated: {by: process:ai-wiki-maintainer}\n---\n"
                       b"# Summary\n\nNew line.\n",
    "sources/waio-612-3f2a.md.source": b"shadow evidence\n",
}
REVISIONS = ("a3583950c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6", "b71c2e0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
             "c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8")


def test_compare_renders_the_golden_side_by_side_diff() -> None:
    rendered = admin.render(BASE, LIVE, SHADOW, names=("solvely-wiki", "solvely-wiki-shadow"), revisions=REVISIONS,
                            width=100)

    assert rendered == GOLDEN.read_text(encoding="utf-8")
    # The same trees in another order render the same bytes.
    assert admin.render(dict(reversed(BASE.items())), dict(reversed(LIVE.items())), dict(reversed(SHADOW.items())),
                        names=("solvely-wiki", "solvely-wiki-shadow"), revisions=REVISIONS, width=100) == rendered


def test_blind_compare_renders_the_golden_diff_without_saying_which_side_is_which() -> None:
    rendered = admin.render(BASE, LIVE, SHADOW, names=("solvely-wiki", "solvely-wiki-shadow"), revisions=REVISIONS,
                            width=100, seed=3)

    assert rendered == BLIND_GOLDEN.read_text(encoding="utf-8")
    # Seed 3 puts shadow on side A for the funnel and live for the new concept.
    assert admin.blind_key(BASE, LIVE, SHADOW, seed=3) == {"metrics/funnel.md": "shadow", "metrics/new.md": "live"}
    # No bundle name, revision or service stamp (generated.by names the flow) is left to tell, nor
    # a source's id or snapshot name (a changeset packet is named <evidence id>-<sha>).
    for tell in ("solvely-wiki", "live", "shadow", REVISIONS[1][:12], REVISIONS[2][:12], "generated", "by:",
                 "waio", "3f2a"):
        assert tell not in rendered.replace("the live flow created", ""), tell
    assert "[^S2]" in rendered  # the footnote labels follow the sources
    flow = b"---\ntitle: X\nsources:\n- {id: 'a-1', resource: /sources/a-1-ff.md.source}\n---\nClaim.[^a-1]\n"
    assert admin._blinded(flow) == b"---\ntitle: X\nsources:\n- {id: 'S1', resource: /sources/S1}\n---\nClaim.[^S1]\n"
    assert len({tuple(admin.blind_key(BASE, LIVE, SHADOW, seed=seed).values()) for seed in range(8)}) > 1


def test_compare_reads_both_published_trees_and_the_base_from_the_writer(owner, capsys) -> None:
    gate = owner
    base = gate.head()
    commit(gate)  # live: the funnel claim
    gate.app(AIWIKI_CHANGESETS_COMMIT="kb-a,kb-b")
    commit(gate, gate.put(METRIC, cited(gate.read(METRIC, "kb-b"), "The funnel moved twice."), bundle="kb-b"),
           gate.put("metrics/probe.md", concept("Probe"), bundle="kb-b"), bundle="kb-b")

    code, out = run(capsys, "admin", "compare", "--live", "kb-a", "--shadow", "kb-b", "--since", base[:12])

    assert code == 0
    lines = out.splitlines()
    assert lines[0] == (f"compare live=kb-a@{gate.head()[:12]} shadow=kb-b@"
                        f"{git(gate.root / 'kb-b', 'rev-parse', 'HEAD')[:12]} since={base[:12]}")
    assert lines[1] == "concepts changed: live 1, shadow 2; differing 2, identical 0"
    assert f"== {METRIC} (live: changed, shadow: changed)" in lines
    assert "== metrics/probe.md (live: absent, shadow: created)" in lines
    assert any("The funnel moved.[^" in line and "|" in line and "The funnel moved twice." in line for line in lines)
    missing = run(capsys, "admin", "compare", "--live", "kb-a", "--shadow", "kb-b", "--since", "0" * 12)
    assert missing[0] == 1 and "not a published revision" in missing[1]

    key = gate.tmp / "key.json"
    code, out = run(capsys, "admin", "compare", "--live", "kb-a", "--shadow", "kb-b", "--since", base[:12],
                    "--blind", str(key), "--seed", "3")
    assert code == 0 and out.splitlines()[:2] == [f"compare blind since={base[:12]}",
                                                  "concepts changed: 2; differing 2, identical 0"]
    assert "kb-a" not in out and "kb-b" not in out
    recorded = json.loads(key.read_text(encoding="utf-8"))
    assert (recorded["seed"], recorded["since"], recorded["live"]["bundle"], recorded["shadow"]["bundle"]) == (
        3, base, "kb-a", "kb-b")
    assert recorded["A"] == {rel: admin._a_side(3, rel) for rel in (METRIC, "metrics/probe.md")}
    assert run(capsys, "admin", "compare", "--live", "kb-a", "--shadow", "kb-b", "--since", base[:12],
               "--seed", "3")[0] == 2  # a seed means nothing without --blind


# --- changesets and revert ----------------------------------------------------------------


def test_revert_waits_out_a_202_and_changesets_shows_who_reverted_what(owner, capsys) -> None:
    gate = owner
    head = gate.head()
    changeset = commit(gate)
    gate.app(AIWIKI_CHANGESET_WAIT_S="0")  # answer 202 at once; the CLI polls the job

    code, out = run(capsys, "admin", "revert", "--changeset", changeset["id"], "--run", "INC-1", "--json")

    assert code == 0, out
    job = document(out)
    assert (job["status"], job["reverted"], job["run"]) == ("done", [changeset["id"]], "INC-1")
    assert gate.calls[0] == ("POST", "/admin/revert", 202) and gate.calls[-1] == ("GET", f"/jobs/{job['id']}", 200)
    assert git(gate.writer, "diff", "--name-only", head, "HEAD", "--", ".", ":!log.md", ":!viz.html") == ""
    code, out = run(capsys, "admin", "changesets", "--principal", OPERATOR)
    assert code == 0
    assert f'"{changeset["id"]}","done","{OPERATOR}",null,' in out and out.rstrip().splitlines()[-1].strip() == (
        '"ai-wiki admin revert --changeset <id>","revert one changeset"')
    assert f',1,"{job["id"]}"' in out
    code, out = run(capsys, "admin", "changesets", "--since", "not a time")
    assert code == 1 and "status 400" in out


def test_revert_exits_7_on_a_conflict_and_2_on_bad_usage(owner, capsys) -> None:
    gate = owner
    changeset = commit(gate)
    other = clone(gate.remote, gate.tmp / "other")
    (other / METRIC).write_text((other / METRIC).read_text().replace("The funnel moved.", "Edited by hand."))
    git(other, "commit", "-qam", "hand edit")
    git(other, "push", "-q", "origin", "main")

    code, out = run(capsys, "admin", "revert", "--changeset", changeset["id"])

    assert code == admin.CONFLICT
    assert 'status: "rejected"' in out and f'"conflict","{METRIC}","{git(other, "rev-parse", "HEAD")}"' in out
    assert run(capsys, "admin", "revert", "--principal", OPERATOR)[0] == 2
    assert run(capsys, "admin", "revert", "--changeset", "nope")[0] == 1  # 404 from the writer


def test_revert_exits_7_and_names_the_problem_that_stopped_it(owner, capsys) -> None:
    gate = owner
    older = commit(gate, gate.put("metrics/probe.md", concept("Probe")))
    visitors = "metrics/web-landing-new-visitor-distribution-2026-09.md"
    linking = cited(gate.read(visitors)).replace("# Summary\n\n", "# Summary\n\nSee [Probe](probe.md).\n\n", 1)
    assert gate.post(gate.request(gate.put(visitors, linking)), token="owner").status_code == 201
    newer = commit(gate, gate.put(METRIC, cited(gate.read(METRIC), "Bad claim.")))

    code, out = run(capsys, "admin", "revert", "--principal", OPERATOR, "--since", "2020-01-01")

    assert code == admin.CONFLICT
    assert f'reverted[1]: "{newer["id"]}"' in out and f'stopped_at: "{older["id"]}"' in out
    assert f'"broken_link","{visitors}",' in out


def test_revert_fails_cleanly_when_its_answer_is_cut_short(owner, capsys, monkeypatch) -> None:
    def cut_short(request, timeout=None):
        raise http.client.IncompleteRead(b"{")

    monkeypatch.setattr(cli.urllib.request, "urlopen", cut_short)

    code, out = run(capsys, "admin", "revert", "--changeset", "5e0c9a1b2f3d")

    assert code == 1 and "cannot reach the configured ai-wiki server" in out


# --- cursor import ---------------------------------------------------------------------------


def test_cursor_import_restores_missing_cursors_and_keeps_differing_ones(owner, capsys, tmp_path) -> None:
    gate = owner
    repos = {"https://code.example/solvely-web-control.git": {"branch": "master", "sha": "1a2b3c4", "stale_since": None,
                                                             "error": None}}
    issues = {"updated_at": "2026-10-16T19:40:00Z", "id": "01a0c068"}
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"run": "WAIO-612", "cursors": {
        "repos": {"name": "repos", "value": repos, "etag": "e1"}, "issues": {"name": "issues", "value": issues}}}))

    code, out = run(capsys, "admin", "cursor", "import", str(report), "--json")

    assert code == 0 and [row["outcome"] for row in json.loads(out)["cursors"]] == ["created", "created"]
    stored = gate.client.get("/maint/cursors/repos", params={"bundle": "kb-a"}, headers=gate.headers()).json()
    assert (stored["value"], stored["updated_by"], stored["run"]) == (repos, "human:owner", "admin:cursor-import")
    assert json.loads(run(capsys, "admin", "cursor", "import", str(report), "--json")[1])["cursors"][0][
        "outcome"] == "unchanged"

    newer = {**repos, "https://code.example/solvely-web-control.git": {**repos[next(iter(repos))], "sha": "5e6f7a8"}}
    report.write_text(json.dumps({"cursors": {"repos": {"value": newer}}}))
    code, out = run(capsys, "admin", "cursor", "import", str(report))
    assert code == 1 and '"repos","kept"' in out and "--replace" in out
    code, out = run(capsys, "admin", "cursor", "import", str(report), "--replace")
    assert code == 0 and '"repos","replaced"' in out
    assert gate.client.get("/maint/cursors/repos", params={"bundle": "kb-a"},
                           headers=gate.headers()).json()["value"] == newer

    for bad in ({"cursors": {"branches": {"value": {}}}}, {"cursors": {"repos": {"value": "x"}}}, {"cursors": {}}):
        report.write_text(json.dumps(bad))
        assert run(capsys, "admin", "cursor", "import", str(report))[0] == 2, bad
    report.write_text("{not json")
    assert run(capsys, "admin", "cursor", "import", str(report))[0] == 2
