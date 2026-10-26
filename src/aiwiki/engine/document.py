"""Shared Open Knowledge Format v0.2 document helpers.

This module is deliberately small: it parses one concept and derives the two
read-side signals defined by OKF v0.2.  Freshness is derived, never written: a concept
without a valid ``stale_after`` goes stale ``DEFAULT_STALE_DAYS`` after its generation, so
the default churns neither Git nor the audit backlog.  The default is a negative signal
only: before it, such a concept reads ``unspecified``, never ``fresh``.  Validation policy lives in
``aiwiki.engine.validate`` so consumers can remain available even when a bundle
contains an invalid document.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_DELIMITER = "---"
# Design §5.4 A4: a process's event confirms its own generation only from a later session, this
# long after it (the gate refuses such a review sooner, and in the run that wrote it).
SELF_REVIEW_DELAY = timedelta(hours=12)

TRUST_UNVERIFIED = "unverified"
TRUST_MACHINE_CONFIRMED = "machine-confirmed"
TRUST_HUMAN_REVIEWED = "human-reviewed"

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_UNSPECIFIED = "unspecified"

# The owner's one default for every type: a generation without its own ``stale_after`` goes
# stale 90 days after it. Only a declared date makes a concept read ``fresh``.
DEFAULT_STALE_DAYS = 90
BASIS_DECLARED, BASIS_DEFAULT, BASIS_NONE = "declared", "default", "none"


class OKFDocumentError(ValueError):
    """Raised when an OKF document cannot be parsed."""


@dataclass(slots=True)
class OKFDocument:
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""


def parse_document(text: str, *, require_frontmatter: bool = True) -> OKFDocument:
    """Parse a UTF-8 OKF markdown document.

    Concept documents require frontmatter.  Reserved files such as ``index.md``
    use ``require_frontmatter=False`` because most indexes intentionally have no
    frontmatter.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        if require_frontmatter:
            raise OKFDocumentError("missing YAML frontmatter")
        return OKFDocument(body=text)

    end = next(
        (i for i, line in enumerate(lines[1:], start=1) if line.strip() == FRONTMATTER_DELIMITER),
        None,
    )
    if end is None:
        raise OKFDocumentError("unterminated YAML frontmatter")

    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
    except (yaml.YAMLError, ValueError) as exc:
        raise OKFDocumentError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise OKFDocumentError("frontmatter must be a mapping")

    body = "\n".join(lines[end + 1:])
    if body.startswith("\n"):
        body = body[1:]
    return OKFDocument(frontmatter=frontmatter, body=body)


def load_document(path: Path, *, require_frontmatter: bool = True) -> OKFDocument:
    return parse_document(path.read_text(encoding="utf-8"), require_frontmatter=require_frontmatter)


def has_symlink_component(root: Path, path: Path) -> bool:
    """Whether ``path`` itself or a bundle-internal ancestor is a symlink.

    Use lexical absolute paths here: resolving first would erase the evidence that
    an untrusted bundle path traversed a link before the read-side gate inspected it.
    Paths outside ``root`` also fail closed.
    """
    root_lexical = Path(os.path.abspath(root))
    path_lexical = Path(os.path.abspath(path))
    try:
        relative = path_lexical.relative_to(root_lexical)
    except ValueError:
        return True
    current = root_lexical
    if current.is_symlink():
        return True
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def scalar_text(value: Any) -> str:
    """Return a stable text form for YAML date/datetime scalars."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value or "")


def normalize_verified(frontmatter: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the allowed bare ``verified`` event to a one-element list."""
    value = frontmatter.get("verified")
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [event for event in value if isinstance(event, dict)]
    return []


def _instant(value: Any) -> datetime | None:
    """Parse an OKF ISO datetime to an aware instant; invalid values fail closed."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else None


def current_verified(frontmatter: dict[str, Any], *,
                     own_after: timedelta | None = SELF_REVIEW_DELAY) -> list[dict[str, Any]]:
    """Verification events that confirm the current generated revision.

    OKF keeps generation and verification history separate.  Once content changes,
    earlier verification is historical evidence, not confirmation of the new revision.
    A document without ``generated.at`` retains the official v0.2 advisory behaviour.
    A process's event on its own generation counts only ``own_after`` past it (design §5.4
    A4: no reviewer verifies its own generation): the 141 auditor corrections stamped verified
    in the act read unverified, and a new run label in the same session confirms nothing; a
    later session's review of that correction (§5.3 ``correction``) counts. A person's
    correction is verified in the act (§5.5), as ever. ``own_after`` None is OKF v0.2's own
    rule, which an older reader applies; ``trust_tier`` still counts every event (§5.3).
    """
    events = normalize_verified(frontmatter)
    generated = frontmatter.get("generated")
    generated_at = _instant(generated.get("at")) if isinstance(generated, dict) else None
    if generated_at is None:
        return events
    author = str(generated.get("by") or "")
    own = generated_at + own_after if own_after is not None and author and not author.startswith("human:") \
        else generated_at
    return [event for event in events if (at := _instant(event.get("at"))) is not None
            and at >= (own if str(event.get("by") or "") == author else generated_at)]


def trust_tier(frontmatter: dict[str, Any]) -> str:
    """Derive the OKF v0.2 advisory trust tier from all verification events.

    Per SPEC §5.3, trust is a property of the ``verified`` history.  Whether
    any event confirms the *current* generated revision is a separate local
    read gate exposed as ``verification_current``.
    """
    events = normalize_verified(frontmatter)
    if not events:
        return TRUST_UNVERIFIED
    if any(str(event.get("by") or "").startswith("human:") for event in events):
        return TRUST_HUMAN_REVIEWED
    return TRUST_MACHINE_CONFIRMED


def declared_stale_date(frontmatter: dict[str, Any]) -> date | None:
    """Parse the declared ``stale_after``, returning None if absent or invalid."""
    raw = frontmatter.get("stale_after")
    if isinstance(raw, datetime):
        return None
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _default_stale_date(frontmatter: dict[str, Any]) -> date | None:
    generated = frontmatter.get("generated")
    at = _instant(generated.get("at")) if isinstance(generated, dict) else None
    try:
        return at.astimezone(UTC).date() + timedelta(days=DEFAULT_STALE_DAYS) if at is not None else None
    except (OverflowError, ValueError):  # a valid time near year 1 or 9999: no default, never a crash
        return None


def freshness_basis(frontmatter: dict[str, Any]) -> str:
    """Where the effective stale date comes from: ``declared``, ``default`` or ``none``."""
    if declared_stale_date(frontmatter) is not None:
        return BASIS_DECLARED
    return BASIS_DEFAULT if _default_stale_date(frontmatter) is not None else BASIS_NONE


def stale_date(frontmatter: dict[str, Any]) -> date | None:
    """The effective stale date: a valid declared ``stale_after``, else the UTC date of
    ``generated.at`` plus ``DEFAULT_STALE_DAYS``, else None."""
    declared = declared_stale_date(frontmatter)
    return declared if declared is not None else _default_stale_date(frontmatter)


def is_stale(frontmatter: dict[str, Any], today: date | None = None) -> bool:
    """A concept is stale on and after its effective stale date."""
    deadline = stale_date(frontmatter)
    return deadline is not None and (today or date.today()) >= deadline


def freshness(frontmatter: dict[str, Any], today: date | None = None) -> str:
    """``stale`` on and after the effective stale date; before it ``fresh`` only under a declared
    ``stale_after``. Before the default date a concept stays ``unspecified``: the default is a
    bookkeeping stamp, not a curator's judgment, so it never lifts one into a fresh read tier."""
    deadline = stale_date(frontmatter)
    if deadline is None:
        return FRESHNESS_UNSPECIFIED
    if (today or date.today()) >= deadline:
        return FRESHNESS_STALE
    return FRESHNESS_FRESH if declared_stale_date(frontmatter) is not None else FRESHNESS_UNSPECIFIED


def generated_fields(frontmatter: dict[str, Any]) -> tuple[str, str]:
    generated = frontmatter.get("generated")
    if not isinstance(generated, dict):
        return "", ""
    return str(generated.get("by") or ""), scalar_text(generated.get("at"))


def _verified_field_values(events: list[dict[str, Any]]) -> tuple[list[str], str]:
    actors = [str(event["by"]) for event in events if event.get("by")]
    timestamped = [
        (instant, scalar_text(event.get("at")))
        for event in events
        if (instant := _instant(event.get("at"))) is not None
    ]
    return actors, max(timestamped, default=(None, ""), key=lambda item: item[0])[1]


def verified_fields(frontmatter: dict[str, Any]) -> tuple[list[str], str]:
    """All verifier actors and the latest verification time (OKF §5.2–5.3)."""
    return _verified_field_values(normalize_verified(frontmatter))


def current_verified_fields(frontmatter: dict[str, Any]) -> tuple[list[str], str]:
    """Verifier actors and latest time that confirm the current revision."""
    return _verified_field_values(current_verified(frontmatter))


def concept_metadata(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Return the compact OKF v0.2 signals shared by every read surface."""
    generated_by, generated_at = generated_fields(frontmatter)
    verified_by, verified_at = verified_fields(frontmatter)
    current_verified_by, current_verified_at = current_verified_fields(frontmatter)
    deadline = stale_date(frontmatter)
    return {
        "status": str(frontmatter.get("status") or "stable"),
        "trust": trust_tier(frontmatter),
        "freshness": freshness(frontmatter),
        "generated_by": generated_by,
        "generated_at": generated_at,
        "verified_by": verified_by,
        "verified_at": verified_at,
        "verification_current": bool(current_verified_by),
        "current_verified_by": current_verified_by,
        "current_verified_at": current_verified_at,
        "last_verification_at": verified_at,
        "stale_after": scalar_text(frontmatter.get("stale_after")),  # as declared
        "stale_on": deadline.isoformat() if deadline else "",
        "freshness_basis": freshness_basis(frontmatter),
    }


def bundle_okf_version(root: Path) -> str:
    """Read ``okf_version`` from the one index allowed to carry frontmatter."""
    index = root / "index.md"
    if has_symlink_component(root, index) or not index.is_file():
        return ""
    try:
        document = load_document(index, require_frontmatter=False)
    except (OSError, UnicodeError, OKFDocumentError):
        return ""
    return scalar_text(document.frontmatter.get("okf_version"))
