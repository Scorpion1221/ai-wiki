"""Secret rules shared by collector redaction and the changeset gate scan.

Each rule is high-precision: a hit blocks a changeset (``secret_detected``) or is
redacted before evidence is frozen, so a rule must match credential shapes, not prose
that merely talks about tokens or passwords. Findings name the rule and the line only;
the matched value is never returned, logged, or echoed.
"""
from __future__ import annotations

import re

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # The whole block when it is complete, so redaction removes the key material too.
    ("private_key", re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
        r"(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----)?")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret_access_key", re.compile(
        r"(?i)\baws_?secret_?access_?key\b[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("webhook_url", re.compile(
        r"https://(?:hooks\.slack\.com/services|open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook)"
        r"/[A-Za-z0-9/_-]{8,}")),
    ("api_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}")),
    ("ai_wiki_token", re.compile(r"\baiw_[a-z]_[A-Za-z0-9_-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # Placeholder passwords (``user:password@``) and ``${VAR}`` templates are documentation.
    ("url_credentials", re.compile(
        r"(?i)(?<=://)[^/\s:@\"'<>]+:"
        r"(?!(?:password|passwd|pass|pwd|secret|changeme|x+|\*+)@)[^/\s@\"'<>{}$]{3,}(?=@[^\s/@])")),
    # A credential-named key assigned a bare random value (a deployment secret's ``"JWT_SECRET": "…"``,
    # ``E2B_API_KEY=…``, ``apiKey: …``): no vendor prefix, so only the key name and the value's shape tell.
    # The value must be 12+ characters mixing letters and digits, so prose, booleans, numbers, ``${VAR}``
    # templates and code expressions (anything with a dot) never match. A hash or an id the key names as one
    # is not a secret; a key word in prose (``tokens: 1234…``) has no assignment shape and is not either.
    # Only the ``value`` group is replaced, so the key name stays readable.
    ("credential_assignment", re.compile(
        r"(?i)(?<![A-Za-z0-9])[\"']?"
        r"(?![A-Za-z0-9_.-]*(?:sha\d*|hash|digest|fingerprint|_id|_ids|name|ref|tokens|count)[\"']?[ \t]*[:=])"
        r"[A-Za-z0-9_.-]*(?:secret|token|api_?key|password|passwd|private_?key|access_?key|auth_?key)"
        r"[A-Za-z0-9_-]*[\"']?[ \t]*[:=][ \t]*[\"']?"
        r"(?P<value>(?=[A-Za-z0-9+/_=-]*[0-9])(?=[A-Za-z0-9+/_=-]*[A-Za-z])[A-Za-z0-9+/_=-]{12,})"
        # A dotted value is a code expression (``process.env.X``, ``config.s2s.key``), never a literal.
        r"(?![A-Za-z0-9+/_=.-])")),
)


# Catch-all rules: a value a specific rule already names is reported (and redacted) once, by that rule.
_FALLBACK = frozenset({"credential_assignment"})


def _span(match: re.Match[str]) -> tuple[int, int]:
    return match.span("value") if "value" in match.re.groupindex else match.span()


def scan(text: str) -> list[tuple[str, int]]:
    """Return sorted ``(rule, 1-based line)`` pairs, one per line where a match starts."""
    found = [(name, match) for name, pattern in RULES for match in pattern.finditer(text)]
    specific = [_span(match) for name, match in found if name not in _FALLBACK]
    findings = {
        (name, text.count("\n", 0, match.start()) + 1)
        for name, match in found
        if name not in _FALLBACK or not any(a < _span(match)[1] and _span(match)[0] < b for a, b in specific)
    }
    return sorted(findings, key=lambda finding: (finding[1], finding[0]))


def redact(text: str) -> tuple[str, int]:
    """Replace every rule match with ``<redacted:rule>``; return the text and the count."""
    count = 0
    for name, pattern in RULES:
        marker = f"<redacted:{name}>"

        def keep_key(match: re.Match[str], marker: str = marker) -> str:
            if "value" not in match.re.groupindex:
                return marker
            start, end = match.start("value") - match.start(), match.end("value") - match.start()
            return match.group(0)[:start] + marker + match.group(0)[end:]

        text, replaced = pattern.subn(keep_key, text)
        count += replaced
    return text, count
