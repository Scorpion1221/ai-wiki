"""Curation runtime — triggers a headless Claude agent to ingest a source into the bundle.

This is the only LLM-using part of ai-wiki. The engine and the read service are
deterministic; curation (prose + judgment) is delegated to `claude -p` running the
okf-knowledge-curator skill. See runtime/curate.py.
"""
