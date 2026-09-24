"""Secret rules: precise enough to block a changeset, never echoing the value."""
from __future__ import annotations

import pytest

from aiwiki.runtime import secrets

# Credential shapes only; each value is synthetic.
LEAKS = {
    "private_key": "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nsynthetic\n-----END OPENSSH " + "PRIVATE KEY-----",
    "aws_access_key_id": "AKIA" + "ABCDEFGHIJKLMNOP",
    "aws_secret_access_key": "aws_secret_access_key = " + "a1B2" * 10,
    "github_token": "ghp_" + "A" * 36,
    "gitlab_token": "glpat-" + "x" * 20,
    "slack_token": "xox" + "b-1234567890-abcdef",
    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/" + "0a1b2c3d-4e5f",
    "api_key": "sk-ant-" + "k" * 40,
    "google_api_key": "AIza" + "Z" * 35,
    "stripe_key": "sk_live_" + "9" * 24,
    "ai_wiki_token": "aiw_c_" + "t" * 32,
    "jwt": ".".join(["eyJ" + "a" * 16, "eyJ" + "b" * 16, "c" * 16]),
    "url_credentials": "postgres://svc:" + "S3cr3tPass@db.internal/app",
}


@pytest.mark.parametrize("rule", sorted(LEAKS))
def test_each_rule_reports_its_line_and_redacts_the_value(rule: str) -> None:
    text = f"# Notes\n\nconfig {LEAKS[rule]} end\n"
    assert secrets.scan(text) == [(rule, 3)]
    redacted, count = secrets.redact(text)
    assert count == 1 and f"<redacted:{rule}>" in redacted
    assert LEAKS[rule] not in redacted and secrets.scan(redacted) == []


def test_prose_placeholders_and_templates_are_not_secrets() -> None:
    prose = (
        "The login flow reads token/UID; api_key 字段 is optional; see password reset.\n"
        "Example DSN: mysql://user:password@host/db or redis://svc:${REDIS_PASSWORD}@cache:6379.\n"
        "sk-learn, AKIA-style ids, https://example.com:8080/a@b and eyJ alone are fine.\n"
    )
    assert secrets.scan(prose) == []
    assert secrets.redact(prose) == (prose, 0)


def test_url_credentials_keep_scheme_and_host() -> None:
    assert secrets.redact(LEAKS["url_credentials"]) == (
        "postgres://<redacted:url_credentials>@db.internal/app", 1,
    )
