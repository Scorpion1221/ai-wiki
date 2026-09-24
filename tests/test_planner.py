"""Planner priorities, noise rules and chunking (design §4.4, W8); the writer keys and ages items."""

from __future__ import annotations

import hashlib

import pytest

from aiwiki.maint import planner
from aiwiki.runtime import secrets


def candidate(collector: str, topic_key: str, **signals: object) -> dict:
    return {"collector": collector, "topic_key": topic_key, "origin": {"kind": collector}, "brief": "b",
            "files": [{"name": "f.md", "sha256": "a" * 64}], "signals": signals}


def test_plan_orders_candidates_by_the_design_priority_table() -> None:
    remote = "repo:code.example.com/web/control"
    candidates = [
        candidate("refresh", "refresh:metrics/a.md"),
        candidate("repos", f"{remote}#src", paths=["src/app.ts"]),
        candidate("issues", "issue:WAIO-7", decision=False, status_changes=0),
        candidate("hygiene", "hygiene:orphan:x.md"),
        candidate("repos", f"{remote}#tasks/h5", paths=["tasks/h5/sql/q.sql"]),
        candidate("repos", f"{remote}#tasks/h5-checkout", paths=["tasks/h5-checkout/a.sql",
                                                                  "tasks/h5-checkout/status.md"]),
        candidate("issues", "issue:WAIO-9", decision=False, status_changes=2),
        candidate("repos", f"{remote}#docs/solutions/cache.md", paths=["docs/solutions/cache.md"]),
        candidate("issues", "issue:WAIO-8#part-1", decision=True, status_changes=0),
        candidate("repos", "rebaseline:code.example.com/web/app"),
        candidate("repos", f"{remote}#memory/learnings.md", paths=["memory/learnings.md"]),
        candidate("inbox", "member:m1"),
    ]

    items = planner.plan(candidates)

    assert [(item["topic_key"].removeprefix(remote), item["priority"]) for item in items] == [
        ("member:m1", 100),
        ("#docs/solutions/cache.md", 80),
        ("#memory/learnings.md", 80),
        ("#tasks/h5-checkout", 70),
        ("issue:WAIO-8#part-1", 60),
        ("issue:WAIO-9", 60),
        ("issue:WAIO-7", 40),
        ("rebaseline:code.example.com/web/app", 40),
        ("#src", 40),
        ("#tasks/h5", 40),
        ("hygiene:orphan:x.md", 30),
        ("refresh:metrics/a.md", 20),
    ]
    assert all("signals" not in item and "item_key" not in item for item in items)  # the writer keys them


def test_plan_keeps_briefs_single_line_and_bounded() -> None:
    long = candidate("repos", "repo:x#src")
    long["brief"] = "line one\nline two " + "x" * 400

    brief = planner.plan([long])[0]["brief"]

    assert brief.startswith("line one line two x")
    assert len(brief) == planner.BRIEF_LIMIT and brief.endswith("…")


def test_plan_redacts_secrets_in_briefs() -> None:
    leaked = candidate("issues", "issue:WAIO-700")
    leaked["brief"] = "WAIO-700 [in_review] Rotate leaked key sk-" + "b" * 32 + "; 0 comments (0 by members)"

    brief = planner.plan([leaked])[0]["brief"]

    assert brief == "WAIO-700 [in_review] Rotate leaked key <redacted:api_key>; 0 comments (0 by members)"


@pytest.mark.parametrize(("path", "kind"), [
    ("package-lock.json", "lockfile"),
    ("web/pnpm-lock.yaml", "lockfile"),
    ("Cargo.lock", "lockfile"),
    ("dist/app.js", "build"),
    ("public/app.min.js", "build"),
    ("vendor/lib/x.go", "vendored"),
    ("src/__tests__/a.ts", "test"),
    ("tests/test_app.py", "test"),
    ("src/app.spec.tsx", "test"),
    ("pkg/server_test.go", "test"),
    (".github/workflows/ci.yml", "ci"),
    (".gitlab-ci.yml", "ci"),
    ("docs/logo.PNG", "binary"),
    ("tasks/h5/report.pdf", "binary"),
    ("config/.env.production", "secret"),
    (".env", "secret"),
    ("tests/fixtures/.env", "secret"),
    ("deploy/id_ed25519", "secret"),
    ("deploy/id_rsa.pub", "secret"),
    ("certs/server.pem", "secret"),
    ("ops/tls.key", "secret"),
    ("ops/credentials-prod.json", "secret"),
    ("docs/environment.md", None),
    ("tasks/h5/README.md", None),
    ("src/app.ts", None),
    ("docs/spec/checkout.md", None),
])
def test_noise_categories(path: str, kind: str | None) -> None:
    assert planner.noise(path) == kind


@pytest.mark.parametrize(("path", "topic"), [
    ("tasks/h5-checkout/README.md", "tasks/h5-checkout"),
    ("tasks/h5-checkout/sql/q.sql", "tasks/h5-checkout"),
    ("tasks/任务/status.md", "tasks/任务"),
    ("memory/learnings.md", "memory/learnings.md"),
    ("docs/solutions/cache.md", "docs/solutions/cache.md"),
    ("docs/guide.md", "docs"),
    ("src/a/b.ts", "src"),
    ("README.md", "."),
    ("tasks", "."),
])
def test_topic_path_groups_task_roots_and_top_directories(path: str, topic: str) -> None:
    assert planner.topic_path(path) == topic


def test_clip_bounds_utf8_bytes_without_splitting_characters() -> None:
    text = "决策" * 1000  # 6000 bytes

    clipped, truncated = planner.clip(text, 1000)

    assert truncated
    assert len(clipped.encode()) <= 1000
    assert clipped.startswith("决策") and clipped.endswith("[truncated from 6000 bytes]\n")
    assert planner.clip("short", 1000) == ("short", False)


def test_pack_is_greedy_and_order_preserving() -> None:
    assert planner.pack(["aa", "bb", "c", "dddd", "e"], 5) == [["aa", "bb", "c"], ["dddd", "e"]]
    assert planner.pack(["toolong", "a"], 5) == [["toolong"], ["a"]]
    assert planner.pack([], 5) == []


def test_evidence_file_redacts_clips_and_hashes_what_it_freezes() -> None:
    text = "token: ghp_" + "a" * 36 + "\nkeep <redacted> as written\n" + "x" * planner.FILE_TEXT_LIMIT

    file = planner.evidence_file("notes.md", text, {"kind": "git-file", "path": "notes.md"}, redactions=2)

    assert file["data"].decode().startswith("token: <redacted:github_token>\nkeep <redacted> as written\n")
    assert file["bytes"] == len(file["data"]) <= planner.FILE_TEXT_LIMIT
    assert file["sha256"] == hashlib.sha256(file["data"]).hexdigest()
    assert file["origin"] == {"kind": "git-file", "path": "notes.md", "truncated": True, "redactions": 3}


PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
SECRETS = {
    "private_key": PEM,
    "aws_access_key_id": "AKIA" + "IOSFODNN7EXAMPLE",
    "ai_wiki_token": "aiw_c_9f8e7d6c5b4a39281706f5e4",
}


def test_evidence_file_uses_the_gate_secret_rules() -> None:
    text = "".join(f"{rule}: {value}\n" for rule, value in SECRETS.items())

    file = planner.evidence_file("keys.md", text, {"kind": "git-file"})

    frozen = file["data"].decode()
    assert frozen == "".join(f"{rule}: <redacted:{rule}>\n" for rule in SECRETS)
    assert file["origin"]["redactions"] == len(SECRETS)
    assert secrets.scan(frozen) == []


@pytest.mark.parametrize("prose", [
    "## Refresh token:\n\nRotate every 30 days\n",
    "Password: at least 8 characters\n",
    "付费墙 token: 30d 过期\n",
    "api_key=${API_KEY}\n",
])
def test_evidence_file_keeps_prose_about_credentials(prose: str) -> None:
    file = planner.evidence_file("notes.md", prose, {"kind": "git-file"})

    assert (file["data"].decode(), file["origin"]["redactions"]) == (prose, 0)
