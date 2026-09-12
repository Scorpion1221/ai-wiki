"""Real CLI subprocesses over loopback HTTP; model/Git outcomes are injected receipts."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def test_cli_and_skill_entry_resume_capacity_failure_over_http(tmp_path):
    jobs, submissions, audits = {}, [], []
    capacity = [False]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer fixture-token"
            self.respond(jobs[urlsplit(self.path).path.split("/")[-1]])

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer fixture-token"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            path = urlsplit(self.path).path
            job_id = f"job-{len(jobs) + 1}"
            job = {"id": job_id, "status": "done", "phase": "done",
                   "validation": {"status": "passed"}, "commit": job_id,
                   "git": {"committed": True, "pushed": True, "commit": job_id},
                   "changed_files": ["features/fixture.md"]}
            if path == "/ingest":
                data = base64.b64decode(payload["content_b64"])
                submissions.append(data)
                job.update(kind="ingest", sha256=hashlib.sha256(data).hexdigest())
                if not capacity[0]:
                    job.update(status="failed", phase="rolled_back", commit=None, git={},
                               error="You've hit your usage limit.", validation={"status": "not_run"},
                               finished=datetime.now(UTC).isoformat())
            else:
                parent = path.split("/")[2]
                assert jobs[parent]["status"] == "done"
                audits.append(parent)
                job.update(kind="audit", parent_job=parent, audit={"status": "passed", "verified_concepts": [],
                                                                  "unverified_concepts": [], "corrected_concepts": []})
            jobs[job_id] = job
            self.respond(job)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"endpoint": f"http://127.0.0.1:{server.server_port}",
                                          "token": "fixture-token", "bundle": "kb"}))
            env = {**os.environ, "AIWIKI_CONFIG": str(config),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            manifest = tmp_path / "manifest.json"
            sources = [tmp_path / "a.md", tmp_path / "b.md"]
            for i, source in enumerate(sources):
                source.write_text(f"immutable source {i}")
            manifest.write_text(json.dumps({"sources": [{"identity": s.name, "path": str(s)} for s in sources]}))
            state_dir = tmp_path / "state"
            args = ["-b", "kb", "maintain", "--state-dir", str(state_dir), "--poll-seconds", "0", "--json"]

            def cli(*extra):
                return subprocess.run([sys.executable, "-m", "aiwiki.cli.main", *args, *extra],
                                      env=env, capture_output=True, text=True, timeout=30)

            first = cli("--manifest", str(manifest))
            assert first.returncode == 1, first.stdout + first.stderr
            assert json.loads(first.stdout)["pending"] == 2
            assert submissions == [b"immutable source 0"] and not audits
            # CLI restart during persisted cooldown makes no write requests.
            assert cli().returncode == 1
            assert submissions == [b"immutable source 0"]
            capacity[0] = True
            recovered = cli("--retry-now")
            assert recovered.returncode == 0, recovered.stdout + recovered.stderr
            assert json.loads(recovered.stdout)["done"] == 2
            assert submissions == [b"immutable source 0", b"immutable source 0", b"immutable source 1"]
            assert len(audits) == 2
            assert cli().returncode == 0 and len(submissions) == 3 and len(audits) == 2
            state = json.loads((state_dir / "state.json").read_text())
            assert [j["status"] for j in state["sources"][0]["ingest"]] == ["failed", "done"]

            # Existing automation's script delegates to the actual CLI, not a stale copy of retry logic.
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            executable = bin_dir / "ai-wiki"
            executable.write_text(f'#!{sys.executable}\nfrom aiwiki.cli.entry import main\nraise SystemExit(main())\n')
            executable.chmod(0o755)
            script = Path(__file__).resolve().parents[1] / "skills/ai-wiki-maintainer/scripts/run_sources.py"
            legacy = subprocess.run([sys.executable, str(script), "--bundle", "kb", "--state-dir", str(state_dir)],
                                    env={**env, "PATH": str(bin_dir) + os.pathsep + env["PATH"]},
                                    capture_output=True, text=True, timeout=30)
            assert legacy.returncode == 0, legacy.stdout + legacy.stderr
            assert json.loads(legacy.stdout)["done"] == 2
            assert len(submissions) == 3 and len(audits) == 2
        finally:
            server.shutdown()
            thread.join(timeout=5)
