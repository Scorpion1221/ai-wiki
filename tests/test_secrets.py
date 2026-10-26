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
    "credential_assignment": '"PLUGIN_INTERNAL_' + 'SECRET": "' + "9f3a" * 16 + '"',
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


@pytest.mark.parametrize("line", [
    '"JWT_' + 'SECRET": "' + "a1b2c3d4e5f6a7b8" + '",',
    "E2B_API_" + "KEY=" + "e2b_" + "0f1e2d3c4b5a69788796a5b4c3d2e1f0aa11bb22",
    "      apiKey: " + "Zx81" * 6,
    "CURSOR_AUTOMATIONS_API_" + "KEY = 'crsr_" + "77ab" * 15 + "'",
])
def test_a_bare_value_under_a_credential_key_is_a_secret_and_the_key_stays(line: str) -> None:
    redacted, count = secrets.redact(line)
    assert count == 1 and "<redacted:credential_assignment>" in redacted
    assert line.split("=")[0].split(":")[0].strip(' "') in redacted  # the key name stays readable
    assert secrets.scan(redacted) == []


@pytest.mark.parametrize("line", [
    "const token = process.env.API_TOKEN || ''",  # a code expression, not a literal
    "s2sApiKey: config.s2s.apiKey || '',",
    "TOKEN_SHA256=" + "ab12" * 16,  # a digest the key names as one
    "secret_id: 01a1349f8d9e7de89bde",
    "Observed raw provider tokens: 1234 across 13 calls",
    "api_key 字段 is optional; set API_KEY=${API_KEY} in .env",
    "password: changeme",
    "JWT_SECRET=true",
])
def test_code_expressions_digests_ids_and_placeholders_are_not_secrets(line: str) -> None:
    assert secrets.scan(line) == []


def test_a_value_a_specific_rule_names_is_reported_once() -> None:
    line = 'GITHUB_TOKEN="ghp_' + "A" * 36 + '"'
    assert secrets.scan(line) == [("github_token", 1)]


def test_url_credentials_keep_scheme_and_host() -> None:
    assert secrets.redact(LEAKS["url_credentials"]) == (
        "postgres://<redacted:url_credentials>@db.internal/app", 1,
    )
