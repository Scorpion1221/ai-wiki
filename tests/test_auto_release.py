"""scripts/auto_release.sh end to end against fakes: a real repository behind a bare remote, and fake `gh`, `ssh`
and `ai-wiki` on PATH that read one JSON state file and record what they were asked to do. The deploy script the
release runs is the repository's own `scripts/deploy_bwg.sh` at main, replaced here by a stub that "deploys" by
writing the state file, so every condition and the rollback are exercised with no GitHub, host or production.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "auto_release.sh"
GATE = ROOT / "scripts" / "release_gate.py"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or shutil.which("git") is None, reason="bash and git are needed")

# The stub deploy: refuses a busy writer like the real one, else points the writer and mirror at the commit.
DEPLOY_STUB = r'''#!/usr/bin/env bash
set -euo pipefail
python3 - "$FAKE_STATE" "$(git -C "$1" rev-parse "$2")" <<'PY'
import json, sys
path, sha = sys.argv[1:]
s = json.load(open(path))
s.setdefault("deploys", []).append(sha)
if s.get("busy", 0) > 0:
    s["busy"] -= 1
    json.dump(s, open(path, "w"))
    sys.exit("ABORT: the writer is busy:")
if sha in s.get("deploy_fails", []):
    json.dump(s, open(path, "w"))
    sys.exit("ABORT: the writer did not come up on " + sha[:7])
s["writer"] = s["mirror"] = sha
json.dump(s, open(path, "w"))
print("DEPLOYED " + sha)
PY
'''
FAKES = {
    "gh": r'''#!/usr/bin/env python3
import json, os, subprocess, sys
p = os.environ["FAKE_STATE"]; s = json.load(open(p)); a = sys.argv[1:]
s.setdefault("calls", []).append("gh " + " ".join(a[:3])); json.dump(s, open(p, "w"))
if a[:2] == ["pr", "view"]:
    print(json.dumps(s["pr"]))
elif a[:1] == ["api"]:
    print(json.dumps(s["checks"]))
elif a[:2] == ["pr", "merge"]:
    head = a[a.index("--match-head-commit") + 1]
    if head != s["pr"]["headRefOid"] or s.get("merge_refused"):
        sys.exit("merge refused")
    # As GitHub does for --merge: a merge commit on main with the head's tree, never a fast-forward.
    g = ["git", "--git-dir", s["origin"], "-c", "user.name=gh", "-c", "user.email=gh@example.com"]
    out = lambda *c: subprocess.run([*g, *c], capture_output=True, text=True, check=True).stdout.strip()
    merge = out("commit-tree", out("rev-parse", head + "^{tree}"), "-p", out("rev-parse", "refs/heads/main"),
                "-p", head, "-m", "Merge pull request")
    out("update-ref", "refs/heads/main", merge)
    s["pr"]["state"] = "MERGED"; json.dump(s, open(p, "w"))
else:
    sys.exit("unexpected gh " + " ".join(a))
''',
    "ssh": r'''#!/usr/bin/env python3
import json, os, sys
p = os.environ["FAKE_STATE"]; s = json.load(open(p))
s.setdefault("calls", []).append("ssh"); json.dump(s, open(p, "w"))
if s.get("host_down"):
    sys.exit(255)
since = any(arg.startswith("SINCE=") and arg != "SINCE=" for arg in sys.argv)
errors = s.get("errors", {}).get(s["writer"], 0) if since else 0
build = s["writer"]  # each of these is a property of the build that is serving
print(f"writer={s['writer']} mirror={s['mirror']} errors={errors} worker={s.get('worker', {}).get(build, 'active')} "
      f"restarts={s.get('restarts', {}).get(build, 0)}")
''',
    "ai-wiki": r'''#!/usr/bin/env python3
import json, os, sys
s = json.load(open(os.environ["FAKE_STATE"])); a = sys.argv[1:]
if a[:1] == ["health"]:
    print(json.dumps({"build": s["mirror"], "compatible": True,
                      "concepts": s.get("concepts", {}).get(s["mirror"], 1264)}))
elif a[:1] == ["search"]:
    print('retrieval:\n  mode: "%s"' % s.get("mode", {}).get(s["mirror"], "hybrid"))
''',
}


def run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp, self.repo, self.origin = tmp, tmp / "repo", tmp / "origin.git"
        self.state, self.log = tmp / "state.json", tmp / "auto-release.log"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / "scripts").mkdir()
        shutil.copy(GATE, self.repo / "scripts" / "release_gate.py")
        self.main = commit(self.repo, {
            "scripts/deploy_bwg.sh": DEPLOY_STUB, "src/aiwiki/__init__.py": "",
            "src/aiwiki/service/__init__.py": "", "src/aiwiki/service/app.py": "LIMIT = 1\n",
            "src/aiwiki/cli/__init__.py": "", "src/aiwiki/cli/search.py": "RETRIES = 2\n"}, "main")
        git(self.repo, "push", "-q", "origin", "HEAD:main")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, text in FAKES.items():
            (self.bin / name).write_text(text, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        self.head = self.propose({"src/aiwiki/cli/search.py": "RETRIES = 3\n"})

    def propose(self, files: dict[str, str], serving: str | None = None) -> str:
        """A release head on top of main, and the world as a green, reviewed, open PR would leave it.
        ``serving`` is the commit production runs: main itself at first, afterwards the last release's head."""
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "checkout", "-q", "-B", "release", self.main)
        head = commit(self.repo, files, "release")
        git(self.repo, "push", "-q", "-f", "origin", "release")
        self.write({"origin": str(self.origin), "writer": serving or self.main, "mirror": serving or self.main,
                    "pr": {"state": "OPEN", "baseRefName": "main", "headRefOid": head, "mergeable": "MERGEABLE",
                           "isDraft": False},
                    "checks": [run("test (3.11)"), run("test (3.12)")]})
        return head

    def write(self, state: dict) -> None:
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def read(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def patch(self, **changes) -> None:
        state = self.read()
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(state.get(key), dict):
                state[key].update(value)
            else:
                state[key] = value
        self.write(state)

    def run(self, verb: str, *, review: str | None = "true", **env: str) -> subprocess.CompletedProcess:
        full = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_STATE": str(self.state),
                "AUTO_RELEASE_LOG": str(self.log), "AUTO_RELEASE_WAIT_S": "0", **env}
        full.pop("AUTO_RELEASE_REVIEW", None)
        if review is not None:
            full["AUTO_RELEASE_REVIEW"] = review
        args = [verb, str(self.repo), "94"] if verb != "verify" else [verb, env.get("SHA", self.main)]
        return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, env=full, timeout=120)

    def main_now(self) -> str:
        return subprocess.run(["git", "--git-dir", str(self.origin), "rev-parse", "refs/heads/main"],
                              capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_a_low_risk_reviewed_green_release_is_merged_deployed_and_verified(world: World) -> None:
    done = world.run("check")
    assert done.returncode == 0 and "conditions hold" in done.stdout, done.stderr
    assert world.main_now() == world.main and "deploys" not in world.read() and not world.log.exists()

    done = world.run("release", review=f"test {world.head} =")  # the review command gets the head appended
    assert done.returncode == 0, done.stderr + done.stdout
    assert f"released: production is healthy on {world.head[:7]} (previous {world.main[:7]})" in done.stdout
    state = world.read()
    merge = world.main_now()  # main holds a merge commit of the head; production serves the head itself
    assert merge != world.head and git(world.repo, "rev-parse", f"{merge}^2") == world.head
    assert state["writer"] == state["mirror"] == world.head and state["deploys"] == [world.head]
    lines = world.log.read_text().splitlines()
    assert [line.split()[-1] for line in lines] == ["merged", "deployed"]
    assert f"head={world.head} previous={world.main}" in lines[0]


@pytest.mark.parametrize(("change", "why"), [
    ({"pr": {"state": "MERGED"}}, "is MERGED"),
    ({"pr": {"isDraft": True}}, "draft: True"),
    ({"pr": {"baseRefName": "develop"}}, "targets develop"),
    ({"pr": {"mergeable": "CONFLICTING"}}, "is CONFLICTING"),
    ({"checks": [run("test (3.11)"), run("test (3.12)", conclusion="failure")]}, "test (3.12) completed/failure"),
    ({"checks": [run("test (3.11)"), run("test (3.12)", "in_progress", None)]}, "test (3.12) in_progress/None"),
    ({"checks": [run("test (3.11)")]}, "1 check(s) ran, 2 expected"),  # one job never ran
    ({"checks": []}, "0 check(s) ran, 2 expected"),
    ({"writer": "0" * 40}, "does not serve main's code"),  # something else is half-deployed
    ({"mirror": "0" * 40}, "does not serve main's code"),
])
def test_a_condition_that_does_not_hold_stops_before_the_merge(world: World, change, why) -> None:
    world.patch(**change)
    for verb in ("check", "release"):
        done = world.run(verb)
        assert done.returncode == 3 and why in done.stderr, (verb, done.stderr)
    assert world.main_now() == world.main and "deploys" not in world.read() and not world.log.exists()
    assert not [call for call in world.read()["calls"] if call.startswith("gh pr merge")]


def test_a_change_the_owner_decides_is_never_merged(world: World) -> None:
    head = world.propose({"src/aiwiki/service/app.py": "LIMIT = 9\n"})
    done = world.run("release")
    assert done.returncode == 3 and "not low-risk" in done.stderr
    assert "ask: src/aiwiki/service/app.py: the writer or the mirror imports it" in done.stderr
    assert world.main_now() == world.main != head and "deploys" not in world.read()


def test_a_release_nobody_reviewed_or_whose_review_refuses_this_head_does_not_go(world: World) -> None:
    done = world.run("release", review=None)
    assert done.returncode == 3 and "AUTO_RELEASE_REVIEW is not set" in done.stderr
    done = world.run("release", review=f"test {'1' * 40} =")  # a record for another head
    assert done.returncode == 3 and "does not approve" in done.stderr
    assert world.run("check", review=None).returncode == 0  # check alone says what it did not look at
    assert world.main_now() == world.main and not world.log.exists()


def test_main_ahead_of_the_release_stops(world: World) -> None:
    git(world.repo, "checkout", "-q", "-B", "other", world.main)
    other = commit(world.repo, {"README.md": "x\n"}, "another change lands on main")
    git(world.repo, "push", "-q", "origin", "other:main")
    world.patch(writer=other, mirror=other)
    done = world.run("release")
    assert done.returncode == 3 and "has commits the PR's head lacks" in done.stderr
    assert world.main_now() == other


def test_one_release_a_day(world: World) -> None:
    assert world.run("release").returncode == 0
    # The next day's state: main is the merge commit, production serves the release head (the same tree).
    first, world.main = world.head, world.main_now()
    second = world.propose({"src/aiwiki/cli/search.py": "RETRIES = 4\n"}, serving=first)
    done = world.run("release")
    assert done.returncode == 3 and "in the last 24 h (limit 1)" in done.stderr
    assert world.main_now() == world.main
    # The owner's knob counts releases, not log lines: the first wrote two lines (merged, deployed) and is one.
    assert world.run("release", AUTO_RELEASE_PER_DAY="2").returncode == 0
    assert world.read()["deploys"] == [second] and world.read()["writer"] == second
    assert f"previous={first} deployed" in world.log.read_text().splitlines()[-1]  # what a rollback would restore
    world.log.write_text(world.log.read_text() + "not-a-date pr=1 head=x previous=y merged\n")
    world.main = world.main_now()
    world.propose({"src/aiwiki/cli/search.py": "RETRIES = 5\n"}, serving=second)
    done = world.run("release", AUTO_RELEASE_PER_DAY="3")  # two dated releases and a line that cannot be dated
    assert done.returncode == 3 and "3 release(s)" in done.stderr


def test_a_busy_writer_past_the_wait_leaves_the_merge_undeployed(world: World) -> None:
    world.patch(busy=5)
    done = world.run("release")
    assert done.returncode == 4 and "stayed busy" in done.stderr
    state = world.read()
    assert git(world.repo, "rev-parse", f"{world.main_now()}^2") == world.head  # merged,
    assert state["writer"] == world.main  # not deployed
    assert [line.split()[-1] for line in world.log.read_text().splitlines()] == ["merged"]


def test_a_release_that_fails_verification_is_rolled_back(world: World) -> None:
    world.patch(errors={world.head: 3})  # the new build logs errors after it starts
    done = world.run("release")
    assert done.returncode == 5, done.stderr
    assert "3 error line(s)" in done.stderr and f"rolled back to {world.main[:7]} and healthy" in done.stderr
    assert "open a revert PR" in done.stderr
    state = world.read()
    assert state["deploys"] == [world.head, world.main] and state["writer"] == state["mirror"] == world.main
    last = world.log.read_text().splitlines()[-1].split()
    assert last[4] == "rolled_back" and last[5].startswith("reason=3_error_line(s)")


@pytest.mark.parametrize(("key", "value", "why"), [
    ("mode", "lexical", "hybrid mode"), ("worker", "failed", "ai-wiki-worker is failed"),
    ("restarts", 2, "restarted 2 time"), ("concepts", 0, "health: concepts"),
])
def test_each_post_deploy_check_can_fail_the_release(world: World, key, value, why) -> None:
    world.patch(**{key: {world.head: value}})  # only the new build is unhealthy, so the rollback verifies
    done = world.run("release")
    assert done.returncode == 5 and why in done.stderr, done.stderr
    assert world.read()["deploys"] == [world.head, world.main] and world.read()["writer"] == world.main


def test_a_failed_rollback_asks_for_a_person(world: World) -> None:
    world.patch(errors={world.head: 1}, deploy_fails=[world.main])
    done = world.run("release")
    assert done.returncode == 6 and "the rollback" in done.stderr and "did not verify" in done.stderr


def test_a_deploy_that_fails_and_restores_itself_reports_the_previous_build(world: World) -> None:
    world.patch(deploy_fails=[world.head])  # deploy_bwg.sh rolled its own part back and exited non-zero
    done = world.run("release")
    assert done.returncode == 5 and f"healthy on the previous {world.main[:7]}" in done.stderr
    assert world.read()["writer"] == world.main and world.main_now() != world.main  # main holds the merge


def test_an_unreachable_host_changes_nothing(world: World) -> None:
    world.patch(host_down=True)
    done = world.run("release")
    assert done.returncode == 2 and "did not answer" in done.stderr
    assert world.main_now() == world.main


def test_verify_alone(world: World) -> None:
    assert world.run("verify", SHA=world.main).returncode == 0
    done = world.run("verify", SHA=world.head)
    assert done.returncode == 3 and f"the writer is on {world.main[:7]}, not {world.head[:7]}" in done.stderr
