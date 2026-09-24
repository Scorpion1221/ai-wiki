"""One retry taxonomy for failed writer jobs.

The writer stamps ``job["failure"]`` where it knows why a job failed. ``classify`` honors
that record and otherwise maps legacy receipt text, so ``ai-wiki maintain`` (which ships in
the same package) never has to guess from prose that newer writers already classified.
"""
from __future__ import annotations

import re

import yaml

from ..engine.document import OKFDocumentError

# class -> (retryable, retry_after_s)
CLASSES: dict[str, tuple[bool, int | None]] = {
    "capacity": (True, 3600),
    "transient": (True, 300),
    "timeout": (True, 300),
    "interrupted": (True, 60),
    "model_output": (True, 300),
    "conflict": (True, 300),
    "auth": (False, None),
    "disk": (False, None),
    "input": (False, None),
    "internal": (True, 300),
}
STAGES = frozenset({"pre_sync", "agent", "repair", "validation", "git", "startup", "intake"})
_PHASE_STAGES = {
    "syncing": "pre_sync", "curating": "agent", "auditing": "agent", "repairing": "repair",
    "before_commit": "git", "committed": "git", "pushed": "git",
}

_SECRETS = (
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1<redacted>"),
    # Env-var names count too (ANTHROPIC_AUTH_TOKEN=, OPENAI_API_KEY=, AWS_SECRET_ACCESS_KEY=):
    # ``_`` is a word character, so a ``\b`` anchor would never see the key word.
    (re.compile(r"(?i)(?<![A-Za-z0-9_])((?:[A-Za-z0-9]+_)*"
                r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|authorization)"
                r"(?:[_-][A-Za-z0-9]+)*[\"']?\s*[:=]\s*[\"']?)(?!<redacted>)[^\s\"',;}]+"), r"\1<redacted>"),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|"
                r"\bgithub_pat_[A-Za-z0-9_]{20,}|\bglpat-[A-Za-z0-9_-]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"),
     "<redacted>"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted>@"),
)

_DISK = re.compile(r"no space left|disk full|\bENOSPC\b", re.I)
_CAPACITY = re.compile(
    r"at capacity|usage limit|insufficient_quota|quota[ _-]?(?:limit|exceeded|exhausted)|"
    r"rate[ _-]limit(?:ed|_exceeded)?|too many requests|\b(?:HTTP|status)\s*[:=]?\s*429\b", re.I)
_AUTH = re.compile(
    r"\b(?:HTTP|status)\s*[:=]?\s*(?:401|403)\b|\bunauthori[sz]ed\b|\bforbidden\b|"
    r"authentication (?:failed|required|error)|invalid (?:api[ _-]?key|token|credentials)|"
    r"not logged in|login required|permission denied", re.I)
_CONFLICT = re.compile(r"rebase conflict|push rejected", re.I)
_TRANSIENT = re.compile(
    r"stream (?:disconnected|error)|\b(?:HTTP|status)\s*[:=]?\s*5\d\d\b|"
    r"\b5\d\d (?:Service Unavailable|Bad Gateway|Gateway Time-?out|Internal Server Error)|"
    r"connection (?:error|reset|refused|failed|closed|aborted)|network (?:error|unreachable)|"
    r"temporarily unavailable|bundle mutation in progress|could not resolve host|cannot reach", re.I)
_TIMEOUT = re.compile(r"timed out|\btimeout\b", re.I)
_SCOPE = re.compile(r"modified (?:files outside|protected Git metadata)", re.I)
_INPUT = re.compile(r"not found|is not done|changed no concept files|scope is missing or invalid|"
                    r"needs conversion", re.I)
# Model-written bytes that failed to parse or broke the concept-scope policy. Their reprs quote
# frontmatter and file names, which must never read as a provider limit.
_MODEL_REPR = re.compile(r"^(?:OKFDocumentError|YAMLError|ScannerError|ParserError|ComposerError|"
                         r"ConstructorError)\(|refusing to apply non-concept agent change")
_REPR = re.compile(r"^\w+(?:Error|Exception)\(")
# Provider and runtime errors are the agent's error lines; transcript lines quote concepts.
_ERROR_LINE = re.compile(r"^\W*(?:error|fatal)\b|\b(?:HTTP|status)\s*[:=]?\s*\d{3}\b", re.I)
_PATH = re.compile(r"\S*[/\\]\S*|\S+\.(?:md|ya?ml|json)\b")


def redact(text: str) -> str:
    """Remove bearer strings, key/token assignments, known token formats and URL credentials."""
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def output_tail(stdout: object, stderr: object, limit: int = 4000) -> str:
    """Redacted last ``limit`` characters of an agent's stderr+stdout (str or bytes)."""
    parts = []
    for value in (stderr, stdout):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return redact("\n".join(parts))[-limit:]


def failure(cls: str, *, stage: str | None = None, detail: object = "",
            retryable: bool | None = None) -> dict:
    """A structured failure record with the contract defaults for ``cls``."""
    default, after = CLASSES[cls]
    retryable = default if retryable is None else retryable
    return {
        "class": cls,
        "retryable": retryable,
        "retry_after_s": after if retryable else None,
        "stage": stage if stage in STAGES else None,
        "detail": redact(str(detail or ""))[:500],
    }


def model_output_error(exc: BaseException) -> bool:
    """Model output that failed to parse or tried to change a non-concept file."""
    return isinstance(exc, (OKFDocumentError, yaml.YAMLError)) or (
        isinstance(exc, RuntimeError) and "refusing to apply non-concept agent change" in str(exc))


def phase_stage(job: dict) -> str | None:
    return _PHASE_STAGES.get(str(job.get("phase") or ""))


def classify(job: dict) -> dict:
    """Return {class, retryable, retry_after_s, stage, detail} for a failed job."""
    structured = job.get("failure")
    if isinstance(structured, dict) and structured.get("class") in CLASSES:
        result = failure(structured["class"], stage=structured.get("stage"),
                         detail=structured.get("detail", ""),
                         retryable=structured.get("retryable") if isinstance(structured.get("retryable"), bool)
                         else None)
        after = structured.get("retry_after_s")
        if result["retryable"] and isinstance(after, int) and not isinstance(after, bool) and after >= 0:
            result["retry_after_s"] = after
        return result
    return _legacy(job)


def _legacy(job: dict) -> dict:
    """Map receipts written before ``job.failure`` existed (contract §2 fallback)."""
    error = str(job.get("error") or "")
    git = job.get("git") if isinstance(job.get("git"), dict) else {}
    git_note = str(git.get("note") or "")
    validation = job.get("validation") if isinstance(job.get("validation"), dict) else {}
    # Nonzero agent exits carry the provider's error at the end of free-form agent output
    # (curate stores it as ``error``, audit as ``stderr``). Earlier transcript lines may quote
    # file names such as "...-login-ab.md", so only the tail can decide a class.
    agent_output = str(job.get("stderr") or "")
    if validation.get("reason") == "curation failed":
        agent_output, error = error, "curation failed"
    agent_failed = bool(agent_output) or error == "adversarial audit failed"
    tail = agent_output[-600:]
    text = " ".join((error, git_note, tail))
    detail = text.strip()[-500:]
    stage = "agent" if agent_failed else phase_stage(job)

    if validation.get("status") == "failed":
        return failure("model_output", stage="validation", detail=error)
    if "interrupted by service restart" in error:
        if job.get("phase") == "rolled_back":
            return failure("interrupted", stage="startup", detail=error)
        return failure("internal", stage="startup", detail=error, retryable=False)
    if _MODEL_REPR.search(error):
        return failure("model_output", stage="validation", detail=error)
    # Limits come from error lines and plain messages, never from file names; capacity (which
    # stops the whole batch) is never read from an exception repr that may quote model output.
    limits = _PATH.sub(" ", " ".join([git_note, *(line for line in tail.splitlines() if _ERROR_LINE.search(line))]))
    message = _PATH.sub(" ", error)
    if _DISK.search(limits) or _DISK.search(message):
        return failure("disk", stage=stage, detail=detail)
    if _CAPACITY.search(limits) or (not _REPR.match(error) and _CAPACITY.search(message)):
        return failure("capacity", stage=stage, detail=detail)
    if _AUTH.search(limits) or _AUTH.search(message):
        return failure("auth", stage=stage, detail=detail)
    if "git commit/push failed" in error or _CONFLICT.search(text):
        return failure("conflict" if _CONFLICT.search(text) else "transient", stage="git", detail=detail)
    if _TRANSIENT.search(text):
        return failure("transient", stage=stage, detail=detail)
    if _TIMEOUT.search(text):
        return failure("timeout", stage="repair" if "repair timed out" in error else (stage or "agent"),
                       detail=detail)
    if _SCOPE.search(error):
        return failure("model_output", stage="agent", detail=error)
    if _INPUT.search(error):
        return failure("input", stage="intake", detail=error)
    return failure("internal", stage=stage, detail=detail)
