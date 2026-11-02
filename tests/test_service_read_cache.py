"""The read path's per-bundle snapshot: reused while the bundle's file stats hold, rebuilt on any
change a read could see, and never a way to serve what a cold read would refuse."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_service_bundle_v02 import _concept

from aiwiki.service import bundle as B
from aiwiki.service import worker

SENTINEL = "P0_CACHE_SENTINEL"


@pytest.fixture(autouse=True)
def _fresh_cache():
    B.drop_snapshots()
    yield
    B.drop_snapshots()


@pytest.fixture
def settled(monkeypatch):
    """Every write so far is older than the racy window, so a scan is reusable."""
    clock = SimpleNamespace(time_ns=lambda: time.time_ns() + 10**9, monotonic_ns=time.monotonic_ns)
    monkeypatch.setattr(B, "time", clock)


def _paths(root: Path) -> list[str]:
    return [rel for _path, rel in B.concepts(root)]


def _rewrite(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_the_snapshot_is_reused_until_the_bundle_changes(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "one")
    _concept(tmp_path, "two")

    first = B._snapshot(tmp_path)
    assert B._snapshot(tmp_path) is first
    assert [record[1] for record in first.records] == _paths(tmp_path) == ["topics/one.md", "topics/two.md"]
    assert list(B._concept_records(tmp_path)) == list(first.records)
    assert B.search(tmp_path, "needle", top_k=None) == B.search(tmp_path, "needle", top_k=None)
    assert B._snapshot(tmp_path) is first


def test_an_edit_is_seen_even_when_size_and_mtime_are_restored(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "one")
    path = tmp_path / "topics" / "one.md"
    _rewrite(path, "needle", "needle aaaa")
    before = path.stat()
    assert B.grep(tmp_path, "aaaa") and B._snapshots[tmp_path].settled

    time.sleep(0.05)  # past any filesystem's timestamp tick
    _rewrite(path, "aaaa", "bbbb")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)

    assert B.grep(tmp_path, "aaaa") == []
    assert [hit["text"] for hit in B.grep(tmp_path, "bbbb")] == ["needle bbbb"]


def test_a_scan_right_after_a_write_is_not_reused(tmp_path: Path, monkeypatch) -> None:
    """A second write in the same timestamp tick can leave every stat as it was, so a scan this
    close to the newest change never stands in for a later request's own scan."""
    _concept(tmp_path, "one")
    path = tmp_path / "topics" / "one.md"
    newest = path.stat().st_ctime_ns
    clock = SimpleNamespace(time_ns=lambda: newest + 1_000_000, monotonic_ns=time.monotonic_ns)
    monkeypatch.setattr(B, "time", clock)

    assert _paths(tmp_path) == ["topics/one.md"]
    assert not B._snapshots[tmp_path].settled
    _rewrite(path, "needle", "bbbbbb")
    assert B.grep(tmp_path, "needle") == [] and B.grep(tmp_path, "bbbbbb")


def test_added_removed_and_renamed_concepts_are_seen(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "one")
    assert _paths(tmp_path) == ["topics/one.md"]

    _concept(tmp_path, "two")
    assert _paths(tmp_path) == ["topics/one.md", "topics/two.md"]
    assert B.health(tmp_path)["concepts"] == 2

    (tmp_path / "topics" / "one.md").unlink()
    assert _paths(tmp_path) == ["topics/two.md"]
    assert [hit["path"] for hit in B.search(tmp_path, "needle")] == ["topics/two.md"]

    (tmp_path / "topics" / "two.md").rename(tmp_path / "topics" / "three.md")
    assert _paths(tmp_path) == ["topics/three.md"]
    (tmp_path / "topics").rename(tmp_path / "areas")
    assert _paths(tmp_path) == ["areas/three.md"]
    assert B.count_concepts(tmp_path) == 1


def test_a_concept_or_directory_swapped_for_a_symlink_disappears(tmp_path: Path, settled) -> None:
    bundle = tmp_path / "bundle"
    outside = tmp_path / "outside"
    bundle.mkdir()
    outside.mkdir()
    _concept(bundle, "one")
    _concept(bundle, "two")
    _concept(outside, SENTINEL.lower())
    assert _paths(bundle) == ["topics/one.md", "topics/two.md"]

    one = bundle / "topics" / "one.md"
    one.unlink()
    one.symlink_to(outside / "topics" / f"{SENTINEL.lower()}.md")
    assert _paths(bundle) == ["topics/two.md"]
    assert B.grep(bundle, SENTINEL) == [] and B.search(bundle, SENTINEL) == []

    (bundle / "topics").rename(tmp_path / "moved")
    (bundle / "topics").symlink_to(tmp_path / "moved", target_is_directory=True)
    assert _paths(bundle) == [] and B.health(bundle)["concepts"] == 0

    (bundle / "index.md").rename(tmp_path / "index.md")
    (bundle / "index.md").symlink_to(tmp_path / "index.md")
    assert B.health(bundle)["okf_version"] == ""


def test_private_paths_never_appear_and_never_invalidate(tmp_path: Path, settled) -> None:
    _concept(tmp_path, "valid")
    snapshot = B._snapshot(tmp_path)
    for private in (".git", "sources", "sources/inbox", ".okf", "topics/.git"):
        (tmp_path / private).mkdir(parents=True, exist_ok=True)
        _concept(tmp_path / private, SENTINEL.lower())

    assert B._snapshot(tmp_path) is snapshot
    B.drop_snapshots()
    assert _paths(tmp_path) == ["topics/valid.md"]
    assert B.grep(tmp_path, SENTINEL) == [] and B.search(tmp_path, SENTINEL) == []


def test_a_writer_mutation_drops_the_snapshot(tmp_path: Path, settled, monkeypatch) -> None:
    """Even a change no file stat shows is read once the mutation that made it ends."""
    _concept(tmp_path, "one")
    assert B._snapshot(tmp_path) is B._snapshot(tmp_path)
    monkeypatch.setattr(B, "_fingerprint", lambda root: ("blind", ()))
    assert B.grep(tmp_path, "needle")

    with worker.serialized_mutation():
        _rewrite(tmp_path / "topics" / "one.md", "needle", "bbbbbb")
        assert B.grep(tmp_path, "bbbbbb") == []  # stats are blind here; readers wait in the service
    assert tmp_path not in B._snapshots
    assert [hit["text"] for hit in B.grep(tmp_path, "bbbbbb")] == ["bbbbbb"]


def test_a_scan_overlapping_a_writer_mutation_is_not_kept(tmp_path: Path, settled, monkeypatch) -> None:
    _concept(tmp_path, "one")
    real_scan = B._scan

    def scan_while_a_writer_finishes(*args):
        snapshot = real_scan(*args)
        B.drop_snapshots()  # what serialized_mutation does as it ends
        return snapshot

    monkeypatch.setattr(B, "_scan", scan_while_a_writer_finishes)
    assert _paths(tmp_path) == ["topics/one.md"]
    assert tmp_path not in B._snapshots


def test_concurrent_readers_during_a_rebuild_all_get_the_whole_bundle(tmp_path: Path, settled, monkeypatch) -> None:
    for n in range(40):
        _concept(tmp_path, f"c{n:02d}")
    assert len(_paths(tmp_path)) == 40
    _concept(tmp_path, "c40")
    expected = [f"topics/c{n:02d}.md" for n in range(41)]

    scans = []
    real_scan, real_profile = B._scan, B._profile_document

    def counted_scan(root, *args):
        scans.append(root)
        return real_scan(root, *args)

    def slow_profile(root, path):
        time.sleep(0.002)  # hold the rebuild open while the other readers arrive
        return real_profile(root, path)

    monkeypatch.setattr(B, "_scan", counted_scan)
    monkeypatch.setattr(B, "_profile_document", slow_profile)
    start = threading.Barrier(8)
    seen: list[list[str]] = []
    errors: list[BaseException] = []

    def read() -> None:
        try:
            start.wait()
            seen.append(_paths(tmp_path))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [] and len(seen) == 8
    assert all(paths == expected for paths in seen)
    assert scans == [tmp_path]  # one rebuild, the other seven waited for it


def test_search_counts_every_match_but_snippets_only_the_rows_it_returns(tmp_path: Path, settled, monkeypatch) -> None:
    for n in range(5):
        _concept(tmp_path, f"c{n}", mentions=n + 1)
    everything = B.search(tmp_path, "needle", top_k=None)
    assert len(everything) == 5

    snippets = []
    real_snippet = B._snippet

    def counted_snippet(body, *args):
        snippets.append(body)
        return real_snippet(body, *args)

    monkeypatch.setattr(B, "_snippet", counted_snippet)
    rows, total = B.search_page(tmp_path, "needle", 2)
    assert (rows, total) == (everything[:2], 5)
    assert len(snippets) == 2

    from test_smoke import AUTH, _client

    response = _client(tmp_path, monkeypatch).get("/search", params={"q": "needle", "top_k": 2}, headers=AUTH).json()
    assert response == {"results": everything[:2], "total": 5}


def test_search_after_an_edit_returns_the_edited_result(tmp_path: Path, settled) -> None:
    """A term's postings are kept per snapshot, so a query run before an edit never answers after it."""
    _concept(tmp_path, "one")
    _concept(tmp_path, "two")
    before = B.search(tmp_path, "needle", top_k=None)
    assert [hit["path"] for hit in before] == ["topics/one.md", "topics/two.md"]
    assert B.search(tmp_path, "haystack") == []

    _rewrite(tmp_path / "topics" / "two.md", "needle", "needle needle needle haystack")
    after = B.search(tmp_path, "needle", top_k=None)
    assert [hit["path"] for hit in after] == ["topics/two.md", "topics/one.md"]
    assert after[0]["score"] > before[1]["score"]
    assert [hit["path"] for hit in B.search(tmp_path, "haystack")] == ["topics/two.md"]

    (tmp_path / "topics" / "two.md").unlink()
    assert [hit["path"] for hit in B.search(tmp_path, "needle", top_k=None)] == ["topics/one.md"]
    assert B.search(tmp_path, "haystack") == []


def test_the_postings_memo_is_bounded(tmp_path: Path, settled, monkeypatch) -> None:
    _concept(tmp_path, "one")
    monkeypatch.setattr(B, "_POSTINGS_MEMO", 2)
    first = B.search(tmp_path, "needle summary test")
    index = B._derived(B._snapshot(tmp_path), B._SearchIndex)
    assert list(index.memo) == ["summary", "test"]
    assert B.search(tmp_path, "needle summary test") == first
