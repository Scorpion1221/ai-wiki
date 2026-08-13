#!/usr/bin/env python3
"""Validate an Open Knowledge Format v0.2 bundle and AI Wiki profile.

``validate_okf_conformance`` implements the deliberately permissive OKF v0.2
interoperability contract.  ``validate_profile`` adds this deployment's producer
contract.  The CLI/default ``validate`` runs both: a bundle may use extension
fields, but it may not keep emitting the superseded v0.1 shape.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .document import OKFDocumentError, load_document

OKF_VERSION = "0.2"
OKF_RESERVED = {"index.md", "log.md"}
PROFILE_STRUCTURAL = {"SCHEMA.md", "purpose.md"}
PROFILE_REQUIRED = ("type", "title", "description", "tags", "status", "generated", "sources")
VALID_STATUS = {"draft", "stable", "deprecated"}
LEGACY_KEYS = {"timestamp", "last_verified_at"}
LEGACY_STATUSES = {"reviewed", "canonical", "stale"}
LEGACY_CITATIONS_RE = re.compile(r"(?m)^# Citations\s*$")
LOG_H2_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ACTOR_RE = re.compile(r"^(?:human:[^\s/]+|process:[^\s/]+|[^\s/]+/[^\s/]+)$")
H1_RE = re.compile(r"^ {0,3}#(?:\s+|$)")
COMPUTATION_H1_RE = re.compile(r"^ {0,3}#\s+Computation(?:\s+#+)?\s*$")
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def parse_doc(path: Path) -> tuple[dict[str, Any], str]:
    """Backward-compatible function name, now parsing only OKF v0.2 documents."""
    if path.is_symlink():
        raise OKFDocumentError("symlinks are not allowed in an OKF bundle")
    document = load_document(path)
    return document.frontmatter, document.body


def _markdown_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames[:] = sorted(
            name for name in dirnames
            if name != ".git" and not (base / name).is_symlink()
        )
        for name in sorted(filenames):
            path = base / name
            if path.suffix == ".md" and not path.is_symlink() and path.is_file():
                files.append(path)
    return sorted(files)


def _bundle_symlink_errors(root: Path) -> list[str]:
    """Find bundle symlinks without following or opening their targets."""
    if root.is_symlink():
        return [".: symlinks are not allowed in an OKF bundle"]
    errors: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        safe_dirs: list[str] = []
        for name in sorted(dirnames):
            path = base / name
            if path.is_symlink():
                errors.append(
                    f"{path.relative_to(root).as_posix()}: symlinks are not allowed in an OKF bundle"
                )
            elif name != ".git":
                safe_dirs.append(name)
        dirnames[:] = safe_dirs
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink():
                errors.append(
                    f"{path.relative_to(root).as_posix()}: symlinks are not allowed in an OKF bundle"
                )
    return errors


def _is_profile_concept(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    return path.name not in OKF_RESERVED | PROFILE_STRUCTURAL and ".okf" not in rel.parts


def should_check(path: Path, root: Path) -> bool:
    """Whether ``path`` is subject to the strict AI Wiki concept profile."""
    return path.suffix == ".md" and _is_profile_concept(path, root)


def _non_empty(value: Any) -> bool:
    return value not in (None, "", [])


def _valid_iso_datetime(value: Any) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is not None
    if not isinstance(value, str) or not value.strip():
        return False
    raw = value.strip()
    # A date alone is not a datetime, and a timezone makes audit events
    # unambiguous across runtimes.
    if "T" not in raw and " " not in raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_iso_date(value: Any) -> bool:
    if isinstance(value, datetime):
        return False
    if isinstance(value, date):
        return True
    if not isinstance(value, str) or not ISO_DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_actor(value: Any, field: str) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{field} must be a non-empty actor"]
    if not ACTOR_RE.fullmatch(value.strip()):
        return [
            f"{field} must use human:<id>, process:<id>, or <producer>/<version>; got {value!r}"
        ]
    return []


def _validate_event(value: Any, field: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{field} must be a mapping with by and at"]
    errors = _validate_actor(value.get("by"), f"{field}.by")
    if not _valid_iso_datetime(value.get("at")):
        errors.append(f"{field}.at must be an ISO 8601 datetime with timezone")
    return errors


def _validate_generated(value: Any) -> list[str]:
    return _validate_event(value, "generated")


def _validate_verified(value: Any) -> list[str]:
    if value is None:
        return []
    events = [value] if isinstance(value, dict) else value
    if not isinstance(events, list) or not events:
        return ["verified must be a mapping or non-empty list of mappings"]
    errors: list[str] = []
    for i, event in enumerate(events):
        errors.extend(_validate_event(event, f"verified[{i}]"))
    return errors


def _validate_sources(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return ["sources must be a non-empty list of mappings"]
    errors: list[str] = []
    source_ids: set[str] = set()
    for i, source in enumerate(value):
        field = f"sources[{i}]"
        if not isinstance(source, dict):
            errors.append(f"{field} must be a mapping; legacy string sources are not supported")
            continue
        resource = source.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            errors.append(f"{field}.resource must be a non-empty string")
        source_id = source.get("id")
        if source_id is not None:
            if not isinstance(source_id, str) or not source_id.strip():
                errors.append(f"{field}.id must be a non-empty string when present")
            elif source_id in source_ids:
                errors.append(f"{field}.id duplicates {source_id!r}")
            else:
                source_ids.add(source_id)
        usage_count = source.get("usage_count")
        if usage_count is not None and (
            not isinstance(usage_count, int) or isinstance(usage_count, bool) or usage_count < 0
        ):
            errors.append(f"{field}.usage_count must be a non-negative integer")
        last_modified = source.get("last_modified")
        if last_modified is not None and not _valid_iso_date(last_modified):
            errors.append(f"{field}.last_modified must be YYYY-MM-DD")
        if "usage_window" in source:
            errors.extend(_validate_usage_window(source.get("usage_window"), f"{field}.usage_window"))
    return errors


def _validate_usage_window(value: Any, field: str = "usage_window") -> list[str]:
    if not isinstance(value, dict):
        return [f"{field} must be a mapping with from and to dates"]
    errors: list[str] = []
    for key in ("from", "to"):
        if not _valid_iso_date(value.get(key)):
            errors.append(f"{field}.{key} must be YYYY-MM-DD")
    return errors


def _fenced_blocks_by_computation_section(body: str) -> list[int]:
    """Count complete fenced blocks under each top-level ``# Computation``.

    The small state machine deliberately ignores headings inside fenced code and
    accepts either backtick or tilde fences. Markdown permits a closing fence to
    be longer than its opener, so counting with a single regex is error-prone.
    """
    sections: list[int] = []
    current_section: int | None = None
    open_fence: tuple[str, int] | None = None

    for line in body.splitlines():
        if open_fence is not None:
            marker, minimum = open_fence
            if re.fullmatch(rf" {{0,3}}{re.escape(marker)}{{{minimum},}}\s*", line):
                if current_section is not None:
                    sections[current_section] += 1
                open_fence = None
            continue

        fence = FENCE_OPEN_RE.match(line)
        if fence:
            token = fence.group(1)
            open_fence = (token[0], len(token))
            continue

        if not H1_RE.match(line):
            continue
        if COMPUTATION_H1_RE.fullmatch(line):
            sections.append(0)
            current_section = len(sections) - 1
        else:
            current_section = None
    return sections


def _validate_resource_mapping(value: Any, field: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{field} must be a mapping with resource"]
    resource = value.get("resource")
    if not isinstance(resource, str) or not resource.strip():
        return [f"{field}.resource must be a non-empty string"]
    return []


def _validate_attested_computation(frontmatter: dict[str, Any], body: str) -> list[str]:
    errors: list[str] = []

    runtime = frontmatter.get("runtime")
    if not isinstance(runtime, str) or not runtime.strip():
        errors.append("Attested Computation.runtime must be a non-empty string")

    errors.extend(_validate_resource_mapping(frontmatter.get("executor"), "Attested Computation.executor"))
    errors.extend(_validate_resource_mapping(frontmatter.get("attester"), "Attested Computation.attester"))

    computation = frontmatter.get("computation")
    has_computation_path = isinstance(computation, str) and bool(computation.strip())
    if computation is not None and not has_computation_path:
        errors.append("Attested Computation.computation must be a non-empty path string")

    sections = _fenced_blocks_by_computation_section(body)
    inline_block_count = sum(sections)
    has_valid_inline_computation = len(sections) == 1 and inline_block_count == 1

    if has_computation_path and sections:
        errors.append(
            "Attested Computation must use computation path or one inline # Computation fence, not both"
        )
    elif not has_computation_path:
        if len(sections) != 1:
            errors.append(
                "Attested Computation without computation path must have exactly one # Computation section"
            )
        if inline_block_count != 1 or not has_valid_inline_computation:
            errors.append("Attested Computation # Computation section must contain exactly one fenced code block")
    return errors


def validate_profile_document(frontmatter: dict[str, Any], body: str) -> list[str]:
    """Validate one concept against the strict AI Wiki OKF v0.2 profile.

    This filesystem-independent helper is shared with read surfaces so an invalid
    or legacy document cannot be presented as trusted knowledge merely because its
    YAML parses.  Returned errors are document-local; bundle validation adds paths.
    """
    errors: list[str] = []
    for key in PROFILE_REQUIRED:
        if not _non_empty(frontmatter.get(key)):
            errors.append(f"missing required frontmatter key {key}")

    for key in ("type", "title", "description"):
        value = frontmatter.get(key)
        if _non_empty(value) and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{key} must be a non-empty string")

    for key in sorted(LEGACY_KEYS & frontmatter.keys()):
        errors.append(f"legacy frontmatter key {key} is not supported")

    status = frontmatter.get("status")
    if status not in VALID_STATUS:
        suffix = " (legacy status is not supported)" if status in LEGACY_STATUSES else ""
        errors.append(f"invalid status {status!r}; expected draft|stable|deprecated{suffix}")

    tags = frontmatter.get("tags")
    if not isinstance(tags, list) or not tags or any(
        not isinstance(tag, str) or not tag.strip() for tag in tags
    ):
        errors.append("tags must be a non-empty list of strings")

    errors.extend(_validate_generated(frontmatter.get("generated")))
    errors.extend(_validate_sources(frontmatter.get("sources")))
    errors.extend(_validate_verified(frontmatter.get("verified")))
    if frontmatter.get("type") == "Attested Computation":
        errors.extend(_validate_attested_computation(frontmatter, body))

    stale_after = frontmatter.get("stale_after")
    if stale_after is not None and not _valid_iso_date(stale_after):
        errors.append("stale_after must be YYYY-MM-DD")
    if "usage_window" in frontmatter:
        errors.extend(_validate_usage_window(frontmatter.get("usage_window")))
    if LEGACY_CITATIONS_RE.search(body):
        errors.append(
            "legacy # Citations section is not supported; "
            "use structured sources and source-id footnotes"
        )
    return errors


def validate_okf_conformance(root: Path) -> list[str]:
    """Validate the exact minimum OKF v0.2 bundle contract."""
    errors = _bundle_symlink_errors(root)
    if root.is_symlink():
        return errors
    for path in _markdown_files(root):
        rel = path.relative_to(root)
        if path.name == "index.md":
            errors.extend(f"{rel}: {error}" for error in _validate_index(path, root))
            continue
        if path.name == "log.md":
            errors.extend(f"{rel}: {error}" for error in _validate_log(path))
            continue
        try:
            document = load_document(path)
        except (OSError, UnicodeError, OKFDocumentError) as exc:
            errors.append(f"{rel}: {exc}")
            continue
        if not _non_empty(document.frontmatter.get("type")):
            errors.append(f"{rel}: missing required frontmatter key type")
    return errors


def _validate_index(path: Path, root: Path) -> list[str]:
    try:
        document = load_document(path, require_frontmatter=False)
    except (OSError, UnicodeError, OKFDocumentError) as exc:
        return [str(exc)]
    is_root = path.parent.resolve() == root.resolve()
    if document.frontmatter and not is_root:
        return ["only the bundle-root index.md may contain frontmatter"]
    if document.frontmatter and set(document.frontmatter) != {"okf_version"}:
        return ["root index.md frontmatter may contain only okf_version"]
    return []


def _validate_log(path: Path) -> list[str]:
    try:
        document = load_document(path, require_frontmatter=False)
    except (OSError, UnicodeError, OKFDocumentError) as exc:
        return [str(exc)]
    errors: list[str] = []
    dates: list[date] = []
    seen: set[date] = set()
    for heading in LOG_H2_RE.findall(document.body):
        if not ISO_DATE_RE.fullmatch(heading):
            errors.append(f"log date heading must be YYYY-MM-DD; got {heading!r}")
            continue
        try:
            parsed = date.fromisoformat(heading)
        except ValueError:
            errors.append(f"log date heading must be a valid calendar date; got {heading!r}")
            continue
        if parsed in seen:
            errors.append(f"log date heading must not be repeated; got {heading!r}")
        seen.add(parsed)
        dates.append(parsed)
    if any(older >= newer for newer, older in zip(dates, dates[1:], strict=False)):
        errors.append("log date headings must be unique and newest first")
    return errors


def validate_profile(root: Path) -> list[str]:
    """Validate the stricter AI Wiki producer profile for OKF v0.2 concepts."""
    errors = _bundle_symlink_errors(root)
    if root.is_symlink():
        return errors
    root_index = root / "index.md"
    if root_index.is_symlink():
        pass  # the filesystem safety error above is sufficient; never read target
    elif not root_index.is_file():
        errors.append("missing root index.md")
    else:
        try:
            index_document = load_document(root_index, require_frontmatter=False)
        except (OSError, UnicodeError, OKFDocumentError) as exc:
            errors.append(f"index.md: {exc}")
        else:
            if str(index_document.frontmatter.get("okf_version") or "") != OKF_VERSION:
                errors.append(f'index.md: okf_version must be "{OKF_VERSION}"')

    for path in _markdown_files(root):
        if not should_check(path, root):
            continue
        rel = path.relative_to(root)
        try:
            document = load_document(path)
        except (OSError, UnicodeError, OKFDocumentError):
            # Exact conformance reports the more useful parser error.
            continue
        frontmatter, body = document.frontmatter, document.body
        errors.extend(f"{rel}: {error}" for error in validate_profile_document(frontmatter, body))

    return errors


def validate(root: Path) -> list[str]:
    """Run exact OKF v0.2 conformance followed by the strict AI Wiki profile."""
    return list(dict.fromkeys(validate_okf_conformance(root) + validate_profile(root)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate an OKF v0.2 bundle.")
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "--conformance-only",
        action="store_true",
        help="check only the permissive official OKF v0.2 interoperability contract",
    )
    args = parser.parse_args(argv)
    root_input = args.bundle.expanduser()
    if root_input.is_symlink():
        print("ERROR: .: symlinks are not allowed in an OKF bundle", file=sys.stderr)
        print("FAILED: 1 error(s)", file=sys.stderr)
        return 1
    root = root_input.resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    errors = validate_okf_conformance(root) if args.conformance_only else validate(root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"FAILED: {len(errors)} error(s)", file=sys.stderr)
        return 1
    mode = "OKF v0.2 conformance" if args.conformance_only else "OKF v0.2 + AI Wiki profile"
    print(f"OK: {root} ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
