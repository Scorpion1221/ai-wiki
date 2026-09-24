"""A writer gate for HTTP tests of POST /changesets and GET /workspace (design §10.1, §10.2).

Bundle ``kb-a`` is a clone of a bare remote seeded from ``fixtures/live_bundle`` and listed
in AIWIKI_CHANGESETS_COMMIT; ``kb-b`` is the same kind of clone, but may only dry-run;
``kb-c`` exists but no agent principal may touch it. The Codex reviewer is replaced by a
recorder, so no agent ever runs.
"""
from __future__ import annotations

import base64
import importlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from aiwiki.engine import scan_sources
from aiwiki.engine.gen_indexes import generate_indexes
from aiwiki.runtime import changeset
from aiwiki.service import auth, worker

LIVE = Path(__file__).parent / "fixtures" / "live_bundle"
METRIC = "metrics/plugin-install-first-payment-funnel-2026-09.md"
AI_STUDY = "experiments/ai-study-deferred-login-ab.md"
AIO_AB = "experiments/web-landing-page-aio-ab.md"
CURATOR = "process:ai-wiki-maintainer"
# The owner's curate-only token on a laptop (design §7): the only kind of curator that uploads.
OPERATOR = "human:operator"
EVIDENCE = b"# Funnel status 2026-09-24\n\nThe plugin funnel moved.\n"
EVIDENCE_ID = "funnel-status-2026-09-24"
TOKENS = {"curator": "aiw_c_curator", "operator": "aiw_h_operator", "auditor": "aiw_a_auditor",
          "owner": "aiw_h_owner", "member": "aiw_m_member", "actorless": "aiw_m_actorless"}
SPECS = {
    "curator": {"id": CURATOR, "scopes": ["read", "submit", "curate"], "bundles": ["kb-a", "kb-b"],
                "limits": {"deprecations_per_day": 2}},
    "operator": {"id": OPERATOR, "scopes": ["read", "submit", "curate"], "bundles": ["kb-a", "kb-b"],
                 "limits": {"deprecations_per_day": 2}},
    "auditor": {"id": "process:ai-wiki-auditor", "scopes": ["read", "audit"], "bundles": ["kb-a", "kb-b"]},
    "owner": {"id": "human:owner", "scopes": ["read", "submit", "curate", "audit", "human_verify", "admin"]},
    "member": {"id": "member:alice", "scopes": ["read", "submit"]},
    "actorless": {"id": "member:legacy-eb17", "scopes": ["read", "submit", "curate"]},  # phase 1 keeps eb17 wide
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def clone(remote: Path, path: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True, capture_output=True)
    git(path, "config", "user.email", "t@local")
    git(path, "config", "user.name", "t")
    return path


def seeded_remote(tmp_path: Path, name: str) -> Path:
    """A bare remote holding a valid bundle built from the live fixture, viz.html tracked."""
    remote = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    seed = clone(remote, tmp_path / f"seed-{name}")
    shutil.copytree(LIVE, seed, dirs_exist_ok=True)
    (seed / "index.md").write_text('---\nokf_version: "0.2"\n---\n# Bundle\n', encoding="utf-8")
    (seed / ".gitignore").write_text(".okf/\nsources/inbox/\n", encoding="utf-8")
    (seed / "viz.html").write_text("<html>generated</html>\n", encoding="utf-8")
    for path in sorted(seed.rglob("*.md")):
        for resource in re.findall(r"resource: (/sources/\S+)", path.read_text(encoding="utf-8")):
            stub = seed / resource.lstrip("/")
            stub.parent.mkdir(parents=True, exist_ok=True)
            stub.write_bytes(f"frozen evidence {stub.name}\n".encode())
    generate_indexes(seed)
    assert scan_sources.main([str(seed), "--commit"]) == 0
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "base")
    git(seed, "push", "-q", "origin", "main")
    return remote


class Gate:
    """The service app over three bundles, a TestClient, and changeset request builders.

    ``remote`` replaces the seeded remote of ``kb-a`` (a bare repository the gate may push to).
    """

    def __init__(self, tmp_path: Path, monkeypatch, remote: Path | None = None, **env: str):
        self.tmp, self.monkeypatch = tmp_path, monkeypatch
        self.root = tmp_path / "bundles"
        self.root.mkdir()
        self.remote = remote or seeded_remote(tmp_path, "kb-a")
        self.writer = clone(self.remote, self.root / "kb-a")
        clone(seeded_remote(tmp_path, "kb-b"), self.root / "kb-b")
        clone(seeded_remote(tmp_path, "kb-c"), self.root / "kb-c")
        principals = tmp_path / "principals.json"
        principals.write_text(json.dumps({"principals": [
            {**spec, "token_sha256": auth.token_sha256(TOKENS[name])} for name, spec in SPECS.items()]}),
            encoding="utf-8")
        self.audits: list[str] = []
        monkeypatch.setattr(worker.audit, "run", lambda _bundle, parent, _job_path: self.audits.append(parent))
        monkeypatch.setattr(worker, "DEFER_POLL_S", 0.05)
        for name in ("COMMIT_BUNDLES", "AUDIT_BUNDLES", "actor_of"):  # the app installs its own; undo that
            monkeypatch.setattr(worker, name, getattr(worker, name))
        monkeypatch.delenv("AIWIKI_GIT", raising=False)
        self.app(AIWIKI_PRINCIPALS=str(principals), **env)

    def app(self, **env: str):
        """(Re)load the service with the fixture's environment plus ``env``."""
        settings = {"AIWIKI_BUNDLES": str(self.root), "AIWIKI_CURATE": "auto", "AIWIKI_DISABLE": "",
                    "AIWIKI_CHANGESETS_COMMIT": "kb-a", "AIWIKI_CHANGESET_WAIT_S": "30", **env}
        self.monkeypatch.delenv("AIWIKI_BUNDLE", raising=False)
        self.monkeypatch.delenv("AIWIKI_DEFAULT_BUNDLE", raising=False)
        for name in ("AIWIKI_AUDIT", "AIWIKI_INTAKE", "AIWIKI_RESTRUCTURE", "AIWIKI_CODEX_AUDIT_MANUAL",
                     "AIWIKI_CHANGESETS_PER_HOUR", "AIWIKI_CHANGESETS_PER_DAY", "AIWIKI_DEPRECATIONS_PER_DAY"):
            self.monkeypatch.delenv(name, raising=False)
        for name, value in settings.items():
            self.monkeypatch.setenv(name, value)
        from aiwiki.service import app as appmod
        self.appmod = importlib.reload(appmod)
        self.client = TestClient(self.appmod.app)
        return self.appmod

    def close(self) -> None:
        """Let queued work (a deferred audit too) drain before the recorder is unpatched."""
        for lease in self.root.glob("*/.okf/maint/lease-*.json"):
            lease.unlink()
        worker._q.join()

    def connect(self, token: str = "curator") -> list[tuple]:
        """Point the ai-wiki CLI at this gate in-process, configured with ``token`` and bundle kb-a.

        Returns the log of ``(method, route, status, body)`` the CLI sent; ``self.client`` is
        read per request, so the CLI follows an ``app()`` reload.
        """
        from aiwiki.cli import main as cli

        config = self.tmp / "cli-config.json"
        config.write_text(json.dumps({"endpoint": "http://gate.test", "token": TOKENS[token], "bundle": "kb-a"}),
                          encoding="utf-8")
        self.monkeypatch.setattr(cli, "CONFIG", config)
        calls: list[tuple] = []

        def http(method, route, *, bundle=None, params=None, data=None, headers=None, timeout=60):
            _endpoint, bearer = cli._conn()
            query = {key: value for key, value in {**(params or {}), "bundle": bundle}.items() if value is not None}
            sent = {"Authorization": f"Bearer {bearer}", **(headers or {})}
            if data is not None:
                sent.setdefault("Content-Type", "application/json")
            response = self.client.request(method, route, params=query, content=data, headers=sent)
            calls.append((method, route, response.status_code, data))
            return response.status_code, response.headers, response.content

        self.monkeypatch.setattr(cli, "_http", http)
        return calls

    # --- requests -------------------------------------------------------------------------

    def headers(self, token: str = "curator", run: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {TOKENS[token]}"}
        if run:
            headers["X-AIWiki-Run"] = run
        return headers

    def post(self, request: object, *, token: str | None = None, bundle: str = "kb-a", run: str | None = None,
             dry_run: bool = False):
        """POST a changeset, by default as the curator that may send its evidence: the maintainer
        cites frozen work items; an upload is a human's (the operator's)."""
        evidence = request.get("evidence") if isinstance(request, dict) else None
        token = token or ("operator" if isinstance(evidence, dict) and "upload" in evidence else "curator")
        params = {"bundle": bundle, **({"dry_run": "true"} if dry_run else {})}
        return self.client.post("/changesets", params=params, json=request, headers=self.headers(token, run))

    def job(self, job_id: str, bundle: str = "kb-a") -> dict:
        return json.loads((self.root / bundle / ".okf" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))

    def jobs(self, bundle: str = "kb-a") -> list[str]:
        return sorted(path.stem for path in (self.root / bundle / ".okf" / "jobs").glob("*.json"))

    def read(self, rel: str, bundle: str = "kb-a") -> str:
        return (self.root / bundle / rel).read_text(encoding="utf-8")

    def request(self, *files: dict, bundle: str = "kb-a", evidence: bytes = EVIDENCE, **extra) -> dict:
        """A curate changeset with an uploaded packet (the operator's); by default METRIC cites it."""
        return {
            "schema": changeset.SCHEMA, "kind": "curate", "intent": "evidence",
            "base_revision": git(self.root / bundle, "rev-parse", "HEAD"),
            "evidence": {"id": EVIDENCE_ID, "upload": {"filename": "status.md",
                                                       "content_b64": base64.b64encode(evidence).decode()}},
            "files": list(files) or [self.put(METRIC, cited(self.read(METRIC, bundle)), bundle=bundle)],
            "message": "curate: plugin funnel status", **extra,
        }

    def put(self, rel: str, content: str, *, bundle: str = "kb-a") -> dict:
        target = self.root / bundle / rel
        base = changeset.content_hash(target.read_text(encoding="utf-8")) if target.is_file() else None
        return {"path": rel, "op": "put", "base": base, "content": content}

    def deprecate(self, rel: str, superseded_by: str = METRIC, *, bundle: str = "kb-a") -> dict:
        return {"path": rel, "op": "deprecate", "base": changeset.content_hash(self.read(rel, bundle)),
                "superseded_by": superseded_by, "reason": "folded into the funnel metric"}

    # --- work items -----------------------------------------------------------------------

    def item(self, text: bytes = EVIDENCE, *, run: str = "WAIO-1", topic: str = "repo:x#tasks/funnel") -> str:
        """Take the maintainer lease for ``run`` and claim one freshly frozen item."""
        queued = self.client.post("/maint/items", params={"bundle": "kb-a"}, headers=self.headers(), json={"items": [{
            "origin": {"kind": "repo"}, "topic_key": topic, "priority": 70, "brief": "funnel status",
            "files": [{"name": "S1-status.md", "content_b64": base64.b64encode(text).decode(),
                       "origin": {"kind": "git-file", "remote": "https://code.example/solvely-web-control.git",
                                  "commit": "1a2b3c4d", "path": "tasks/funnel/status.md"}}]}]})
        assert queued.status_code == 200, queued.text
        lease = self.client.post("/maint/lease/maintainer", params={"bundle": "kb-a"}, headers=self.headers(run=run))
        assert lease.status_code == 200, lease.text
        claimed = self.client.post("/maint/items/next", params={"bundle": "kb-a"}, headers=self.headers(run=run))
        return claimed.json()["item"]["id"]

    def item_request(self, item_id: str, *files: dict, **extra) -> dict:
        request = self.request(*files, work_items=[item_id], **extra)
        request["evidence"] = {"id": EVIDENCE_ID, "item_files": [f"{item_id}/S1-status.md"]}
        return request

    # --- git state ------------------------------------------------------------------------

    def head(self) -> str:
        return git(self.writer, "rev-parse", "HEAD")

    def remote_head(self) -> str:
        return git(self.remote, "rev-parse", "main")

    def assert_untouched(self, head: str) -> None:
        """No commit reached the remote and the writer clone is clean at ``head``."""
        assert self.remote_head() == head and self.head() == head
        assert git(self.writer, "status", "--porcelain") == ""
        assert not list((self.writer / "sources" / "inbox").glob("*"))


def cited(text: str, claim: str = "The funnel moved.") -> str:
    """The concept with this changeset's packet cited and one new claim."""
    lines = text.splitlines(keepends=True)
    start = lines.index("sources:\n")
    end = next(i for i in range(start + 1, len(lines)) if re.match(r"[A-Za-z_]+:|---", lines[i]))
    lines.insert(end, f"- {{id: {EVIDENCE_ID}, resource: evidence:packet}}\n")
    return "".join(lines).replace("# Summary\n\n", f"# Summary\n\n{claim}[^{EVIDENCE_ID}]\n\n", 1)


def cite(text: str, evidence_id: str = EVIDENCE_ID, claim: str = "The funnel moved.") -> str:
    """``text`` citing the packet in its own list indentation, with one new footnoted claim."""
    lines = text.splitlines(keepends=True)
    start = lines.index("sources:\n")
    end = next(i for i in range(start + 1, len(lines)) if re.match(r"[A-Za-z_]+:|---", lines[i]))
    indent = re.match(r" *", lines[start + 1]).group()
    lines.insert(end, f"{indent}- {{id: {evidence_id}, resource: evidence:packet}}\n")
    return "".join(lines).rstrip("\n") + f"\n\n{claim}[^{evidence_id}]\n\n[^{evidence_id}]: the packet\n"


def concept(title: str, extra: str = "", body: str = "The funnel moved.") -> str:
    """A new Metric concept that cites this changeset's packet."""
    return (f"---\ntype: Metric\ntitle: {title}\ndescription: A probe concept\ntags: [metric]\n{extra}"
            f"sources:\n- {{id: {EVIDENCE_ID}, resource: evidence:packet}}\n---\n# Summary\n\n"
            f"{body}[^{EVIDENCE_ID}]\n\n[^{EVIDENCE_ID}]: status file\n")


def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)
