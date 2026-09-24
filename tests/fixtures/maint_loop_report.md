AI Wiki maintenance WAIO-9 (<time>) status=done
collect: repos ok 1 scanned (changed 1, new 0, rebaselined 0, failed 0), 4 items | issues ok 1 changed, 1 items | enqueued 5 (merged 0, duplicate 0)
cursors: repos <time> (1 repos) -> <time> (1 repos) | issues (<time>, 0) -> (<time>, issue-7)
queue: taken 7 | curated 1 | skipped 1 | duplicate 2 | parked 2 (context 1, model_output 1) | split 1 | returned 0 | remaining 2 (queue_empty)
changesets: <h1> <h2> metrics/probe-retry-cap.md
gate: first submission passed 1/1; yaml_parse x1
audit backlog: 0 pending, 1 queued, oldest 0h (codex); resubmitted 0
needs human: none

items:
- it_<h3> issue:WAIO-7: skipped (no_durable_knowledge)
- it_<h4> repo:file://<tmp>/control#docs: split (into it_<h5>, it_<h6>)
- it_<h5> repo:file://<tmp>/control#docs#split-1: duplicate (duplicate_of:metrics/probe-retry-cap.md)
- it_<h6> repo:file://<tmp>/control#docs#split-2: duplicate (duplicate_of:metrics/probe-retry-cap.md)
- it_<h7> repo:file://<tmp>/control#memory/learnings.md: curated (changeset <h1>)
- it_<h8> repo:file://<tmp>/control#src: parked (model_output 1/3)
- it_<h9> repo:file://<tmp>/control#tasks/funnel: parked (context 1/3)

cursor JSON (off-site copy for `ai-wiki admin cursor import`):
```json
{"cursors": {"issues": {"etag": "<h10>", "name": "issues", "run": "WAIO-9", "updated_at": "<time>", "updated_by": "process:ai-wiki-maintainer", "value": {"id": "issue-7", "updated_at": "<time>"}}, "repos": {"etag": "<h11>", "name": "repos", "run": "WAIO-9", "updated_at": "<time>", "updated_by": "process:ai-wiki-maintainer", "value": {"file://<tmp>/control": {"branch": "main", "error": null, "sha": "<h12>", "stale_since": null}}}}}
```
