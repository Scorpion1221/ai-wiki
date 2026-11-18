"""A finished run's workspace (a full bundle checkout, ~340 MB) is deleted at the end of a later run.

2026-11-18 on the Multica runtime the 352 run folders under ~/.ai-wiki/state held 28 GB, 26 GB of
it in their ``ws/``, growing ~2.5 GB a day: no run reads another run's workspace.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from aiwiki.cli import maint

HOUR = 3600


def run_folder(root: Path, name: str, *, age_hours: float, ws: bool = True) -> Path:
    folder = root / name
    (folder / "ws" / "metrics").mkdir(parents=True) if ws else folder.mkdir(parents=True)
    record = folder / "run.json"
    record.write_text("{}", encoding="utf-8")
    if ws:
        (folder / "ws" / "metrics" / "x.md").write_bytes(b"x" * 2048)
    stamp = time.time() - age_hours * HOUR
    for path in [record, *(folder.rglob("*"))]:
        os.utime(path, (stamp, stamp))
    os.utime(folder, (stamp, stamp))
    return folder


def test_finished_workspaces_go_but_the_newest_and_the_live_stay(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AIWIKI_KEEP_WORKSPACES", raising=False)
    root = tmp_path / "runs"
    old = [run_folder(root, f"old-{i}", age_hours=10 + i) for i in range(5)]
    recent = run_folder(root, "recent", age_hours=0.5)  # wrote within the lease TTL: maybe still running
    named = run_folder(root, maint._slug("WAIO-1"), age_hours=48)
    (root / "current-kb.json").write_text(json.dumps({"run": "WAIO-1", "bundle": "kb"}), encoding="utf-8")
    bare = run_folder(root, "no-ws", age_hours=72, ws=False)

    result = maint.prune_workspaces(tmp_path, "runs", keep=3)

    assert result == {"removed": 2, "freed_mb": 0}
    assert [(folder / "ws").is_dir() for folder in old] == [True, True, True, False, False]  # the 3 newest stay
    assert all((folder / "run.json").is_file() for folder in old)  # the records always stay
    assert (recent / "ws").is_dir() and (named / "ws").is_dir() and bare.is_dir()
    assert maint.prune_workspaces(tmp_path, "runs", keep=3) == {"removed": 0, "freed_mb": 0}  # idempotent


def test_the_setting_keeps_more_or_turns_it_off(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "reviews"
    folders = [run_folder(root, f"r-{i}", age_hours=10 + i) for i in range(4)]
    monkeypatch.setenv("AIWIKI_KEEP_WORKSPACES", "-1")
    assert maint.prune_workspaces(tmp_path, "reviews") == {"removed": 0, "freed_mb": 0}
    monkeypatch.setenv("AIWIKI_KEEP_WORKSPACES", "1")
    assert maint.prune_workspaces(tmp_path, "reviews")["removed"] == 3
    assert [(folder / "ws").is_dir() for folder in folders] == [True, False, False, False]
    assert maint.prune_workspaces(tmp_path / "absent", "runs") == {"removed": 0, "freed_mb": 0}


def test_a_symlinked_workspace_is_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.md").write_text("keep", encoding="utf-8")
    folder = run_folder(tmp_path / "runs", "linked", age_hours=48, ws=False)
    (folder / "ws").symlink_to(outside, target_is_directory=True)
    assert maint.prune_workspaces(tmp_path, "runs", keep=0) == {"removed": 0, "freed_mb": 0}
    assert (outside / "keep.md").is_file()
