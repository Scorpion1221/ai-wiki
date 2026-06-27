"""ai-wiki HTTP service — read API over an OKF bundle (token-authed).

MVP: read-only endpoints (health/ls/cat/grep/search/log) that an `ai-wiki` CLI client
calls so agents don't need a local clone. Run:

    AIWIKI_BUNDLE=<path> AIWIKI_TOKEN=<token> python -m aiwiki.service
"""
