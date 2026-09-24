"""Server-owned maintenance state (design §2.10, §10.1): leases, cursors, items and caps."""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aiwiki.service import maint_state as M

P = "process:ai-wiki-maintainer"
AUTH = {"Authorization": "Bearer testtok"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(M, "_now", clock)
    monkeypatch.setattr(M, "build", lambda: "build-1")
    return clock


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    path = tmp_path / "kb"
    (path / ".okf" / "jobs").mkdir(parents=True)
    (path / "index.md").write_text('---\nokf_version: "0.2"\n---\n\n# kb\n', encoding="utf-8")
    return path


def _file(name: str, text: str) -> dict:
    return {"name": name, "content_b64": base64.b64encode(text.encode()).decode(),
            "origin": {"kind": "git-file", "path": f"tasks/{name}"}}


def _item(topic: str, *files: dict, priority: int = 70) -> dict:
    return {"origin": {"kind": "repo", "commit": "1a2b3c4"}, "topic_key": topic, "priority": priority,
            "brief": f"changes in {topic}", "files": list(files) or [_file("S1-README.md", topic)]}


def _enqueue(bundle: Path, *items: dict) -> list[dict]:
    return M.enqueue(bundle, list(items), principal=P)["items"]


def _begin(bundle: Path, run: str) -> dict:
    return M.acquire_lease(bundle, "maintainer", principal=P, run=run)


def _end(bundle: Path, run: str) -> dict:
    return M.release_lease(bundle, "maintainer", principal=P, run=run)


def _next(bundle: Path, run: str) -> dict | None:
    return M.next_item(bundle, principal=P, run=run)["item"]


def _resolve(bundle: Path, item_id: str, run: str, **body) -> dict:
    return M.resolve(bundle, item_id, body, principal=P, run=run)


def _park(bundle: Path, item_id: str, run: str, cls: str) -> dict:
    return _resolve(bundle, item_id, run, outcome="parked", **{"class": cls, "reason": f"{cls} failure"})


def _tree(bundle: Path) -> dict[str, bytes]:
    root = bundle / ".okf" / "maint"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_failed_writes_leave_the_previous_record_and_no_partial_file(bundle, clock, monkeypatch):
    [created] = _enqueue(bundle, _item("repo:x#a"))
    _begin(bundle, "WAIO-1")
    item_json = bundle / ".okf" / "maint" / "items" / created["id"] / "item.json"
    before = item_json.read_bytes()
    real_replace = M.os.replace

    def replace(source, target):
        if Path(target).name == "item.json":
            raise OSError("No space left on device")
        real_replace(source, target)

    monkeypatch.setattr(M.os, "replace", replace)
    with pytest.raises(OSError):
        _next(bundle, "WAIO-1")
    assert item_json.read_bytes() == before  # still ready, attempts untouched
    assert not list((bundle / ".okf" / "maint").rglob("*.tmp"))

    # A create interrupted after its evidence blobs but before item.json is invisible, and
    # the collector's retry produces the same item.
    with pytest.raises(OSError):
        _enqueue(bundle, _item("repo:x#b"))
    assert M.list_items(bundle)["total"] == 1
    monkeypatch.setattr(M.os, "replace", real_replace)
    [retried] = _enqueue(bundle, _item("repo:x#b"))
    assert retried["result"] == "created" and M.list_items(bundle)["total"] == 2
    assert M.read_file(bundle, retried["id"], "S1-README.md") == b"repo:x#b"
    assert _next(bundle, "WAIO-1")["id"] == created["id"]
    assert not list((bundle / ".okf" / "maint").rglob("*.tmp"))


def test_expired_lease_returns_its_items_to_ready(bundle, clock):
    [a] = _enqueue(bundle, _item("repo:x#a"))
    _begin(bundle, "WAIO-1")
    assert _next(bundle, "WAIO-1")["id"] == a["id"]
    # A lost response is harmless: the run gets its in-progress item back without a new attempt.
    again = M.next_item(bundle, principal=P, run="WAIO-1")
    assert again["resumed"] and again["item"]["attempts"]["started"] == 1
    # So is a retried begin: re-taking its own live lease only renews it, and the run keeps its item.
    assert _begin(bundle, "WAIO-1") | {"lease": None} == {"lease": None, "interrupted": [], "unparked": [],
                                                           "build_retry": []}
    assert M.next_item(bundle, principal=P, run="WAIO-1")["resumed"]

    with pytest.raises(M.MaintError) as held:
        _begin(bundle, "WAIO-2")
    assert held.value.status == 409 and held.value.code == "lease_held"
    assert {k: held.value.extra[k] for k in ("holder", "run")} == {"holder": P, "run": "WAIO-1"}
    assert held.value.extra["expires_at"] == "2026-10-16T23:00:00Z"

    clock.now += timedelta(hours=3, seconds=1)
    with pytest.raises(M.MaintError) as expired:
        _next(bundle, "WAIO-1")
    assert expired.value.code == "lease_required"

    begun = _begin(bundle, "WAIO-2")
    assert begun["interrupted"] == [a["id"]]
    item = M.get_item(bundle, a["id"])
    assert item["status"] == "ready" and item["current_run"] is None
    assert item["attempts"]["started"] == 1 and item["attempts"]["counted"] == 0
    assert item["attempts"]["history"][-1] | {"detail": ""} == {
        "run": "WAIO-1", "class": "interrupted", "detail": "", "at": "2026-10-16T23:00:01Z", "build": "build-1"}

    assert _next(bundle, "WAIO-2")["attempts"]["started"] == 2
    # Every write call carrying the run renews its lease, so a long run keeps it.
    clock.now += timedelta(hours=2)
    M.enqueue(bundle, [_item("repo:x#z")], principal=P, run="WAIO-2")
    clock.now += timedelta(hours=2)
    assert M.require_lease(bundle, "maintainer", principal=P, run="WAIO-2")["run"] == "WAIO-2"
    released = M.release_lease(bundle, "maintainer", principal=P, run="WAIO-2")
    assert released == {"released": True, "interrupted": [a["id"]]}
    assert M.get_item(bundle, a["id"])["status"] == "ready"
    assert _end(bundle, "WAIO-2")["released"] is False


def test_attempt_caps_move_an_item_to_needs_human_without_blocking_others(bundle, clock):
    [a, b] = _enqueue(bundle, _item("repo:x#a", priority=90), _item("repo:x#b", priority=10))
    for index, run in enumerate(("WAIO-1", "WAIO-2", "WAIO-3"), 1):
        _begin(bundle, run)
        assert _next(bundle, run)["id"] == a["id"]
        parked = _park(bundle, a["id"], run, "model_output")
        if index == 1:
            # A parked item waits for the next run; the independent item is served meanwhile.
            assert parked["status"] == "parked"
            assert _next(bundle, run)["id"] == b["id"]
            _resolve(bundle, b["id"], run, outcome="skipped", reason="no_durable_knowledge")
        _end(bundle, run)
    item = M.get_item(bundle, a["id"])
    assert item["status"] == "needs_human" and item["attempts"]["counted"] == 3
    assert item["resolution"]["reason"] == "attempt_cap"

    _begin(bundle, "WAIO-4")
    [c] = _enqueue(bundle, _item("repo:x#c"))
    assert _next(bundle, "WAIO-4")["id"] == c["id"]
    snapshot = M.status(bundle)
    assert [row["id"] for row in snapshot["needs_human"]] == [a["id"]]
    assert snapshot["items"]["needs_human"]["count"] == 1 and snapshot["items"]["in_progress"]["count"] == 1

    # Uncounted classes (transient, capacity, conflict, interrupted) are bounded by starts.
    [t] = _enqueue(bundle, _item("repo:x#t", priority=1))
    _resolve(bundle, c["id"], "WAIO-4", outcome="skipped", reason="out_of_scope")
    _end(bundle, "WAIO-4")
    for index in range(1, 9):
        run = f"WAIO-T{index}"
        _begin(bundle, run)
        assert _next(bundle, run)["id"] == t["id"]
        parked = _park(bundle, t["id"], run, "transient")
        assert parked["status"] == ("needs_human" if index == 8 else "parked")
        _end(bundle, run)
    assert parked["attempts"]["counted"] == 0 and parked["attempts"]["started"] == 8

    # A class P0 marks non-retryable (auth, disk, input) needs a human at once.
    [n] = _enqueue(bundle, _item("repo:x#n"))
    _begin(bundle, "WAIO-N")
    assert _next(bundle, "WAIO-N")["id"] == n["id"]
    parked = _park(bundle, n["id"], "WAIO-N", "input")
    assert parked["status"] == "needs_human" and parked["resolution"]["reason"] == "not_retryable"
    assert parked["attempts"]["started"] == 1


def test_a_new_gate_build_earns_one_extra_attempt(bundle, clock, monkeypatch):
    [a] = _enqueue(bundle, _item("repo:x#a"))
    for run in ("WAIO-1", "WAIO-2", "WAIO-3"):
        _begin(bundle, run)
        _next(bundle, run)
        _park(bundle, a["id"], run, "context")
        _end(bundle, run)
    assert M.get_item(bundle, a["id"])["status"] == "needs_human"
    assert _begin(bundle, "WAIO-4")["build_retry"] == []
    _end(bundle, "WAIO-4")

    monkeypatch.setattr(M, "build", lambda: "build-2")
    assert _begin(bundle, "WAIO-5")["build_retry"] == [a["id"]]
    assert _next(bundle, "WAIO-5")["id"] == a["id"]
    assert _park(bundle, a["id"], "WAIO-5", "model_output")["status"] == "needs_human"
    _end(bundle, "WAIO-5")
    assert _begin(bundle, "WAIO-6")["build_retry"] == []

    reopened = M.admin_retry(bundle, a["id"], principal="human:owner", reason="fixed the source")
    assert reopened["status"] == "ready" and reopened["attempts"]["counted"] == 0
    assert reopened["reopened"][-1]["from"] == "needs_human"



def test_item_key_is_stable_over_file_order_and_tracks_evidence():
    key = M.item_key("repos", "repo:x#src", ["b", "a"])

    assert key == M.item_key("repos", "repo:x#src", ["a", "b"])
    assert key != M.item_key("repos", "repo:x#src", ["a", "c"])
    assert key != M.item_key("issues", "repo:x#src", ["a", "b"])
    assert key != M.item_key("repos", "repo:x#docs", ["a", "b"])


def test_aging_adds_five_per_whole_day_waited_so_nothing_starves(bundle, clock):
    now = clock.now

    def aged(priority: int, age: timedelta) -> int:
        return M._effective_priority({"priority": priority, "created_at": M._iso(now - age)}, now)

    assert [aged(40, age) for age in (timedelta(0), timedelta(hours=23, minutes=59), timedelta(days=1),
                                      timedelta(days=12, hours=5), -timedelta(days=1))] == [40, 40, 45, 100, 40]
    waiting = {"repo:x#new-issue": (60, timedelta(hours=1)), "repo:x#old-code": (40, timedelta(days=5)),
               "repo:x#new-member": (100, timedelta(0)), "repo:x#starved": (20, timedelta(days=17))}
    for topic, (priority, age) in waiting.items():
        clock.now = now - age
        _enqueue(bundle, _item(topic, priority=priority))
    clock.now = now
    _begin(bundle, "WAIO-1")
    served = []
    while (item := M.next_item(bundle, principal=P, run="WAIO-1")["item"]) is not None:
        served.append(item["topic_key"])
        M.resolve(bundle, item["id"], {"outcome": "skipped", "reason": "out_of_scope"}, principal=P, run="WAIO-1")

    # 20 + 17*5 = 105 > 100 > 40 + 5*5 = 65 > 60
    assert served == ["repo:x#starved", "repo:x#new-member", "repo:x#old-code", "repo:x#new-issue"]

def test_item_key_is_idempotent_and_a_ready_topic_absorbs_newer_evidence(bundle, clock, monkeypatch):
    first = _item("repo:x#a", _file("S1-README.md", "v1"))
    [a] = _enqueue(bundle, first)
    sha = hashlib.sha256(b"v1").hexdigest()
    assert a["item_key"] == M.item_key("repo", "repo:x#a", [sha]) and a["id"] == "it_" + a["item_key"][:12]
    [again] = _enqueue(bundle, first)
    assert again == {**a, "result": "duplicate"}

    newer = _item("repo:x#a", _file("S1-README.md", "v2"), _file("S2-status.md", "done"), priority=80)
    [merged] = _enqueue(bundle, newer)
    assert merged["result"] == "merged" and merged["id"] == a["id"]
    item = M.get_item(bundle, a["id"])
    assert [f["name"] for f in item["files"]] == ["S1-README.md", "S2-status.md"]
    assert item["priority"] == 80 and item["versions"][0]["replaced"][0]["sha256"] == sha
    assert M.read_file(bundle, a["id"], "S1-README.md") == b"v2"
    assert (bundle / ".okf" / "maint" / "items" / a["id"] / "files" / sha).read_bytes() == b"v1"
    assert _enqueue(bundle, newer)[0]["result"] == "duplicate"

    _begin(bundle, "WAIO-1")
    _next(bundle, "WAIO-1")
    # An item in progress is never merged into: newer evidence becomes its own item.
    [later] = _enqueue(bundle, _item("repo:x#a", _file("S1-README.md", "v3")))
    assert later["result"] == "created" and later["id"] != a["id"]
    _resolve(bundle, a["id"], "WAIO-1", outcome="skipped", reason="duplicate_of:decisions/x.md")
    # A closed item still dedupes its collection; nothing is re-queued.
    assert _enqueue(bundle, first)[0] | {"status": None} == {**a, "result": "duplicate", "status": None}
    assert M.list_items(bundle, status="ready")["total"] == 1

    assert M.close_curated(bundle, [later["id"]], job_id="5e0c9a1b2f3d", commit="c9d0", principal=P,
                           run="WAIO-1") == [later["id"]]
    assert M.get_item(bundle, later["id"])["resolution"]["job"] == "5e0c9a1b2f3d"
    assert M.close_curated(bundle, [later["id"], "it_000000000000"], job_id="other", commit=None, principal=P,
                           run=None) == []

    # A parked item is still waiting too, so its topic absorbs newer evidence rather than
    # queueing a second item for the same topic.
    [p] = _enqueue(bundle, _item("repo:x#p", _file("S1-README.md", "p1")))
    assert _next(bundle, "WAIO-1")["id"] == p["id"]
    _park(bundle, p["id"], "WAIO-1", "transient")
    [merged] = _enqueue(bundle, _item("repo:x#p", _file("S2-status.md", "p2")))
    assert merged == {**p, "item_key": merged["item_key"], "result": "merged", "status": "parked"}
    _end(bundle, "WAIO-1")
    assert _begin(bundle, "WAIO-2")["unparked"] == [p["id"]]
    [ready] = M.list_items(bundle, status="ready")["items"]
    assert ready["id"] == p["id"] and [f["name"] for f in ready["files"]] == ["S1-README.md", "S2-status.md"]

    # A merge that would overflow the item queues a new item instead of failing the batch, so
    # a full item never wedges the collector.
    monkeypatch.setattr(M, "MAX_FILES", 2)
    results = _enqueue(bundle, _item("repo:x#q"), _item("repo:x#p", _file("S3-report.md", "p3")))
    assert [r["result"] for r in results] == ["created", "created"] and results[1]["id"] != p["id"]
    assert len(M.get_item(bundle, p["id"])["files"]) == 2


def test_evidence_reads_the_bytes_a_changeset_named_and_leases_expire(bundle, clock):
    [first] = _enqueue(bundle, _item("repo:x#a", _file("S1-README.md", "v1")))
    [merged] = _enqueue(bundle, _item("repo:x#a", _file("S1-README.md", "v2")))  # same topic, still ready
    assert merged["result"] == "merged" and merged["id"] == first["id"]
    v1 = hashlib.sha256(b"v1").hexdigest()

    assert M.evidence(bundle, first["id"], "S1-README.md")[1] == b"v2"
    meta, data = M.evidence(bundle, first["id"], "S1-README.md", v1)  # a changeset queued before the merge
    assert (data, meta["sha256"]) == (b"v1", v1)
    with pytest.raises(M.MaintError) as missing:
        M.evidence(bundle, first["id"], "S1-README.md", "0" * 64)
    assert missing.value.status == 404

    assert M.active_lease(bundle, "maintainer") is None
    _begin(bundle, "WAIO-1")
    assert M.active_lease(bundle, "maintainer")["run"] == "WAIO-1"
    clock.now += M.LEASE_TTL
    assert M.active_lease(bundle, "maintainer") is None


def test_cursor_cas_lets_exactly_one_concurrent_writer_win(bundle, clock):
    with pytest.raises(M.MaintError) as missing:
        M.put_cursor(bundle, "repos", {}, if_match=None, if_none_match=None, principal=P)
    assert missing.value.status == 428
    created = M.put_cursor(bundle, "repos", {"r": {"sha": "a"}}, if_match=None, if_none_match="*", principal=P)
    with pytest.raises(M.MaintError) as exists:
        M.put_cursor(bundle, "repos", {}, if_match=None, if_none_match="*", principal=P)
    assert exists.value.status == 412

    writers = 8
    barrier = threading.Barrier(writers)
    outcomes: list[object] = [None] * writers

    def write(index: int) -> None:
        barrier.wait()
        try:
            outcomes[index] = M.put_cursor(bundle, "repos", {"r": {"sha": str(index)}},
                                           if_match=f'"{created["etag"]}"', if_none_match=None, principal=P)
        except M.MaintError as exc:
            outcomes[index] = exc.status

    threads = [threading.Thread(target=write, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [o for o in outcomes if isinstance(o, dict)]
    assert len(winners) == 1 and outcomes.count(412) == writers - 1
    current = M.get_cursor(bundle, "repos")
    assert current == winners[0] and current["etag"] != created["etag"]


def test_split_closes_the_parent_and_queues_disjoint_children(bundle, clock):
    [parent] = _enqueue(bundle, _item("repo:x#big", _file("S1-a.md", "a"), _file("S2-b.md", "b"),
                                      _file("S3-c.md", "c"), priority=60))
    _begin(bundle, "WAIO-1")
    _next(bundle, "WAIO-1")
    for children in ([{"files": ["S1-a.md"]}],
                     [{"files": ["S1-a.md", "S2-b.md"]}, {"files": ["S2-b.md"]}],
                     [{"files": ["S1-a.md"]}, {"files": ["missing.md"]}],
                     # every file lands in a child, so a split cannot drop evidence ...
                     [{"files": ["S1-a.md"]}, {"files": ["S2-b.md"]}],
                     # ... and children never merge back into one item with fresh counters
                     [{"files": ["S1-a.md"], "topic_key": "t"}, {"files": ["S2-b.md", "S3-c.md"], "topic_key": "t"}]):
        with pytest.raises(M.MaintError) as bad:
            _resolve(bundle, parent["id"], "WAIO-1", outcome="split", children=children)
        assert bad.value.status == 400
    assert M.get_item(bundle, parent["id"])["status"] == "in_progress"

    closed = _resolve(bundle, parent["id"], "WAIO-1", outcome="split", reason="two topics",
                      children=[{"files": ["S1-a.md", "S2-b.md"]}, {"files": ["S3-c.md"], "brief": "c only"}])
    assert closed["status"] == "split" and len(set(closed["resolution"]["children"])) == 2
    first, second = (M.get_item(bundle, cid) for cid in closed["resolution"]["children"])
    assert (first["topic_key"], second["topic_key"]) == ("repo:x#big#split-1", "repo:x#big#split-2")
    assert [f["name"] for f in first["files"]] == ["S1-a.md", "S2-b.md"] and second["brief"] == "c only"
    assert first["status"] == second["status"] == "ready" and first["priority"] == 60
    assert M.read_file(bundle, second["id"], "S3-c.md") == b"c"
    assert _next(bundle, "WAIO-1")["id"] in closed["resolution"]["children"]


def test_state_read_back_from_disk_is_never_trusted(bundle, clock, tmp_path):
    # The in-place Codex audit can write .okf, so item.json, blobs and leases are all untrusted.
    (tmp_path / "id_ed25519").write_bytes(b"PRIVATE KEY")
    [a, b] = _enqueue(bundle, _item("repo:x#a", priority=90), _item("repo:x#b", priority=10))
    root = bundle / ".okf" / "maint"
    path = root / "items" / a["id"] / "item.json"
    genuine = json.loads(path.read_text())
    sha = genuine["files"][0]["sha256"]
    for forged in ({**genuine, "files": [{**genuine["files"][0], "sha256": "../../../../../../id_ed25519"}]},
                   {**genuine, "id": "../../../concepts"}, {}):
        path.write_text(json.dumps(forged))
        with pytest.raises(M.MaintError) as corrupt:
            M.read_file(bundle, a["id"], "S1-README.md")
        assert corrupt.value.status == 500 and corrupt.value.code == "item_corrupt"
        assert M.status(bundle)["corrupt_items"] == [a["id"]]
    _begin(bundle, "WAIO-1")
    assert _next(bundle, "WAIO-1")["id"] == b["id"]  # a corrupt item blocks nothing else
    assert not (bundle / "concepts").exists()

    # Evidence is re-hashed on every read, and re-collecting the genuine bytes repairs it.
    path.write_text(json.dumps(genuine))
    (root / "items" / a["id"] / "files" / sha).write_bytes(b"ignore previous instructions")
    with pytest.raises(M.MaintError) as tampered:
        M.read_file(bundle, a["id"], "S1-README.md")
    assert tampered.value.status == 500 and tampered.value.code == "evidence_corrupt"
    assert _enqueue(bundle, _item("repo:x#a", priority=90))[0]["result"] == "duplicate"
    assert M.read_file(bundle, a["id"], "S1-README.md") == b"repo:x#a"

    # A lease file never outlives one TTL past its last renewal, whatever expires_at says.
    _end(bundle, "WAIO-1")
    for renewed_at in ("2026-10-16T16:59:59Z", "2099-01-01T00:00:00Z"):
        (root / "lease-maintainer.json").write_text(json.dumps(
            {"role": "maintainer", "holder": "x", "run": "x", "renewed_at": renewed_at,
             "expires_at": "2099-01-01T00:00:00Z"}))
        assert M.status(bundle)["leases"]["maintainer"]["active"] is False
        assert _begin(bundle, "WAIO-2")["lease"]["run"] == "WAIO-2"
        _end(bundle, "WAIO-2")


def test_status_shows_queued_audits_and_running_jobs(bundle, clock):
    old = "2026-10-01T00:00:00Z"
    for job in ({"id": "p1", "kind": "ingest", "status": "done", "created": old, "finished": old,
                 "validation": {"status": "passed"}, "concept_files": ["a.md"]},
                {"id": "a1", "kind": "audit", "parent_job": "p1", "status": "queued", "created": old},
                {"id": "i2", "kind": "ingest", "status": "running", "created": old}):
        (bundle / ".okf" / "jobs" / f"{job['id']}.json").write_text(json.dumps(job))
    snapshot = M.status(bundle)
    # A queued (lease-deferred) audit is not pending, but the watchdog still sees its age.
    assert snapshot["audit"] | {"mode": None} == {"mode": None, "pending": 0, "oldest_finished": None,
                                                  "queued": 1, "oldest_queued": old}
    assert snapshot["jobs"] == {"queued": 1, "running": 1}


def _client(bundle: Path, monkeypatch, disable: str = "", curate: str = "auto"):
    principals = bundle.parent / "principals.json"
    principals.write_text(json.dumps({"principals": [{
        "id": "human:owner", "token_sha256": hashlib.sha256(b"testtok").hexdigest(),
        "scopes": ["read", "submit", "curate", "audit", "human_verify", "admin"]}]}), encoding="utf-8")
    monkeypatch.setenv("AIWIKI_BUNDLE", str(bundle))
    monkeypatch.delenv("AIWIKI_BUNDLES", raising=False)
    monkeypatch.setenv("AIWIKI_PRINCIPALS", str(principals))
    monkeypatch.setenv("AIWIKI_CURATE", curate)
    monkeypatch.setenv("AIWIKI_DISABLE", disable)
    from aiwiki.service import app as appmod
    importlib.reload(appmod)
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


def test_maint_http_contract(bundle, monkeypatch):
    client = _client(bundle, monkeypatch)
    r1, r2 = {**AUTH, "X-AIWiki-Run": "WAIO-1"}, {**AUTH, "X-AIWiki-Run": "WAIO-2"}
    assert client.post("/maint/items", json={"items": [_item("repo:x#a")]}).status_code == 401
    queued = client.post("/maint/items", json={"items": [_item("repo:x#a")]}, headers=AUTH)
    assert queued.status_code == 200 and queued.json()["created"] == 1
    item_id = queued.json()["items"][0]["id"]
    assert client.post("/maint/items", json={"items": [{"topic_key": "t"}]}, headers=AUTH).status_code == 400

    assert client.post("/maint/lease/maintainer", headers=r1).json()["lease"]["run"] == "WAIO-1"
    conflict = client.post("/maint/lease/maintainer", headers=r2)
    assert conflict.status_code == 409
    assert {"holder", "run", "expires_at"} <= conflict.json()["detail"].keys()
    assert client.post("/maint/lease/nobody", headers=r1).status_code == 404

    assert client.post("/maint/items/next", headers=AUTH).json()["detail"]["code"] == "lease_required"
    claimed = client.post("/maint/items/next", headers=r1).json()
    assert claimed["item"]["id"] == item_id and claimed["ready"] == 0
    assert client.get(f"/maint/items/{item_id}/files/S1-README.md", headers=AUTH).content == b"repo:x#a"
    added = client.post(f"/maint/items/{item_id}/files", json=_file("S2-extra.md", "more"), headers=r1)
    assert [f["name"] for f in added.json()["files"]] == ["S1-README.md", "S2-extra.md"]
    skipped = client.post(f"/maint/items/{item_id}/resolve", headers=r1,
                          json={"outcome": "skipped", "reason": "no_durable_knowledge"})
    assert skipped.json()["status"] == "skipped"
    assert client.post("/maint/items/next", headers=r1).json()["item"] is None
    listed = client.get("/maint/items", params={"status": "skipped"}, headers=AUTH).json()
    assert listed["total"] == 1 and not listed["truncated"]

    assert client.put("/maint/cursors/repos", json={"value": {}}, headers=AUTH).status_code == 428
    put = client.put("/maint/cursors/repos", json={"value": {"r": 1}}, headers={**r1, "If-None-Match": "*"})
    assert put.status_code == 200 and put.headers["ETag"] == f'"{put.json()["etag"]}"'
    assert client.put("/maint/cursors/repos", json={"value": {"r": 2}},
                      headers={**AUTH, "If-Match": '"stale"'}).status_code == 412
    assert client.get("/maint/cursors/repos", headers=AUTH).headers["ETag"] == put.headers["ETag"]
    assert client.get("/maint/cursors/other", headers=AUTH).status_code == 404

    status = client.get("/maint/status", headers=AUTH).json()
    assert status["items"]["skipped"]["count"] == 1 and status["leases"]["maintainer"]["active"]
    assert status["cursors"]["repos"]["run"] == "WAIO-1" and status["audit"]["mode"] == "codex"
    reopened = client.post(f"/admin/items/{item_id}/retry", headers=AUTH)
    assert reopened.json()["status"] == "ready"
    closed = client.post(f"/admin/items/{item_id}/resolve", headers=AUTH,
                         json={"outcome": "needs_access", "reason": "share the doc with the app"})
    assert closed.json()["resolution"]["admin"] is True
    assert client.delete("/maint/lease/maintainer", headers=r1).json()["released"] is True

    disabled = _client(bundle, monkeypatch, disable="maint,admin")
    assert disabled.get("/maint/status", headers=AUTH).status_code == 403
    assert disabled.post(f"/admin/items/{item_id}/retry", headers=AUTH).status_code == 403



def test_a_token_without_an_actor_writes_no_maintenance_state(bundle, monkeypatch):
    # The legacy shared token holds every scope but names nobody: it may read the state, never
    # take a run's lease (which would hold the bundle's Codex audits back), feed the queue or
    # close an item.
    monkeypatch.setenv("AIWIKI_BUNDLE", str(bundle))
    monkeypatch.delenv("AIWIKI_BUNDLES", raising=False)
    monkeypatch.setenv("AIWIKI_TOKEN", "testtok")
    monkeypatch.setenv("AIWIKI_CURATE", "auto")
    monkeypatch.setenv("AIWIKI_DISABLE", "")
    from aiwiki.service import app as appmod
    importlib.reload(appmod)
    from fastapi.testclient import TestClient
    client = TestClient(appmod.app)
    [item] = _enqueue(bundle, _item("repo:x#a"))
    run = {**AUTH, "X-AIWiki-Run": "anyone-1"}

    for method, route, body in (
            ("POST", "/maint/lease/maintainer", None), ("DELETE", "/maint/lease/maintainer", None),
            ("PUT", "/maint/cursors/repos", {"value": {}}), ("POST", "/maint/items", {"items": [_item("repo:x#b")]}),
            ("POST", "/maint/items/next", None), ("POST", f"/maint/items/{item['id']}/files", _file("S2.md", "x")),
            ("POST", f"/maint/items/{item['id']}/resolve", {"outcome": "skipped", "reason": "out_of_scope"}),
            ("POST", f"/admin/items/{item['id']}/retry", {}),
            ("POST", f"/admin/items/{item['id']}/resolve", {"outcome": "skipped", "reason": "out_of_scope"})):
        response = client.request(method, route, json=body, headers={**run, "If-None-Match": "*"})
        assert response.status_code == 403 and "no actor" in response.json()["detail"], route

    assert M.active_lease(bundle, "maintainer") is None
    assert [i["status"] for i in M.list_items(bundle)["items"]] == ["ready"]
    assert client.get("/maint/status", headers=AUTH).status_code == 200

def test_maint_http_errors_are_structured(bundle, monkeypatch):
    client = _client(bundle, monkeypatch)
    r1 = {**AUTH, "X-AIWiki-Run": "WAIO-1"}
    queued = client.post("/maint/items", headers=AUTH,
                         json={"items": [_item("repo:x#a", priority=90), _item("repo:x#b", priority=10)]})
    a, b = (row["id"] for row in queued.json()["items"])
    client.post("/maint/lease/maintainer", headers=r1)
    assert client.post("/maint/items/next", headers=r1).json()["item"]["id"] == a

    # Malformed input is a 400 the client fixes, never a 5xx it would retry.
    for body in ({"outcome": ["parked"]}, {"outcome": "parked", "class": ["x"]},
                 {"outcome": "parked", "class": "interrupted"}):
        bad = client.post(f"/maint/items/{a}/resolve", headers=r1, json=body)
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "input"
    assert client.post(f"/admin/items/{a}/resolve", headers=AUTH, json={"outcome": {}}).status_code == 400

    retry = client.post(f"/admin/items/{a}/retry", headers=AUTH)
    assert retry.status_code == 409 and retry.json()["detail"]["code"] == "not_reopenable"
    assert retry.json()["detail"]["status"] == "in_progress"
    body = {"outcome": "skipped", "reason": "no_durable_knowledge"}
    assert client.post(f"/maint/items/{a}/resolve", headers=r1, json=body).status_code == 200
    # A resend after a lost response, or a call on an item the run does not hold, is a 409 that
    # names the item's current status.
    for item_id, response, status in (
            (a, client.post(f"/maint/items/{a}/resolve", headers=r1, json=body), "skipped"),
            (a, client.post(f"/maint/items/{a}/files", headers=r1, json=_file("S2-extra.md", "more")), "skipped"),
            (b, client.post(f"/maint/items/{b}/resolve", headers=r1, json=body), "ready")):
        assert response.status_code == 409, item_id
        assert response.json()["detail"] | {"message": ""} == {
            "code": "item_not_in_progress", "message": "", "status": status, "current_run": None}
    closed = client.post(f"/admin/items/{a}/resolve", headers=AUTH, json={"outcome": "needs_access", "reason": "x"})
    assert closed.status_code == 409 and closed.json()["detail"]["code"] == "item_closed"

    # A read deployment (AIWIKI_CURATE=off) refuses rather than answer from an empty state.
    mirror = _client(bundle, monkeypatch, curate="off")
    assert mirror.get("/maint/status", headers=AUTH).status_code == 403
    assert mirror.post(f"/admin/items/{a}/retry", headers=AUTH).status_code == 403
