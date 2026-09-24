"""Curation write-boundary policy shared by every curation writer.

These checks decide what one curation pass may change in a bundle: only concept files,
no source rewrites, service-owned trust bookkeeping, and provenance to the pass's own
evidence. They were moved verbatim from ``curate``; the only change is the ``actor``
parameter of ``_curation_policy_errors``, so a writer other than the Codex curator can
be held to the same rules under its own ``generated.by``. The isolated workspace copy
the checks run against lives here too, so every writer judges the same knowledge tree.

This module must stay import-light: it never imports ``curate`` (which loads the agent
config at import time), so a client can run the same policy without the Codex runner.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from ..engine.document import current_verified, normalize_verified
from ..engine.scan_sources import _source_resource_rel
from ..engine.validate import parse_doc, should_check

CURATOR_ACTOR = "process:ai-wiki-curator"


def _isolated_agent_bundle(bundle: Path) -> tuple[Path, Path]:
    """Copy only knowledge content into a disposable agent workspace.

    Git metadata and service-owned lifecycle state never enter the workspace, so the
    agent cannot mutate them even if its prompt or path handling goes wrong.
    """
    temporary = Path(tempfile.mkdtemp(prefix="ai-wiki-agent-"))
    workspace = temporary / "bundle"
    bundle_resolved = bundle.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        base = Path(directory).resolve()
        ignored: set[str] = set()
        if base == bundle_resolved:
            ignored.update(name for name in (".git", ".okf") if name in names)
        if base == bundle_resolved / "sources" and "inbox" in names:
            ignored.add("inbox")
        return ignored

    shutil.copytree(bundle, workspace, symlinks=True, ignore=ignore)
    return temporary, workspace


def _agent_tree_snapshot(bundle: Path) -> dict[str, bytes]:
    """Snapshot knowledge content; service-owned .okf/inbox state is concurrent."""
    snapshot: dict[str, bytes] = {}
    for directory, dirnames, filenames in os.walk(bundle, followlinks=False):
        base = Path(directory)
        # Git internals are not bundle content and never enter the Codex workspace.
        dirnames[:] = [
            name for name in dirnames
            if name != ".git"
            and not (base == bundle and name == ".okf")
            and not (base == bundle / "sources" and name == "inbox")
            and not (base / name).is_symlink()
        ]
        for name in filenames:
            if name == ".git" and base == bundle:
                continue
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            snapshot[path.relative_to(bundle).as_posix()] = path.read_bytes()
    return snapshot


def _agent_symlink_snapshot(bundle: Path) -> dict[str, str]:
    """Record in-bundle symlinks without resolving or reading their targets."""
    links: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(bundle, followlinks=False):
        base = Path(directory)
        if ".git" in dirnames:
            dirnames.remove(".git")
        if base == bundle and ".okf" in dirnames:
            dirnames.remove(".okf")
        if base == bundle / "sources" and "inbox" in dirnames:
            dirnames.remove("inbox")
        for name in list(dirnames) + filenames:
            path = base / name
            if path.is_symlink():
                links[path.relative_to(bundle).as_posix()] = os.readlink(path)
    return links


def _agent_scope_errors(
    bundle: Path,
    before: dict[str, bytes],
    links_before: dict[str, str],
    source_rel: str,
    expected_sha: str | None,
) -> list[str]:
    """Allow only concept edits; the pre-existing service snapshot is read-only."""
    after = _agent_tree_snapshot(bundle)
    links_after = _agent_symlink_snapshot(bundle)
    errors = [
        f"{rel}: curation may not create, remove, or retarget symlinks"
        for rel in sorted(set(links_before) | set(links_after))
        if links_before.get(rel) != links_after.get(rel)
    ]
    changes = sorted(
        rel for rel in set(before) | set(after)
        if before.get(rel) != after.get(rel)
    )
    new_snapshots: list[str] = []
    for rel in changes:
        path = bundle / rel
        if rel in links_before or rel in links_after:
            continue  # already rejected above; never classify a symlink as a concept
        if rel in after and path.is_file() and should_check(path, bundle):
            continue
        parts = Path(rel).parts
        if parts and parts[0] == "sources" and "inbox" not in parts:
            if rel in before and rel not in after:
                errors.append(f"{rel}: curation deleted immutable source evidence")
            elif rel in before:
                errors.append(f"{rel}: curation modified immutable source evidence")
            elif expected_sha and hashlib.sha256(after[rel]).hexdigest() == expected_sha:
                new_snapshots.append(rel)
            else:
                errors.append(f"{rel}: curation added source evidence unrelated to this ingest")
            continue
        errors.append(f"{rel}: curation modified a prohibited non-concept bundle file")
    for rel in new_snapshots[1:]:
        errors.append(f"{rel}: curation created more than one snapshot for this ingest")
    return errors


def _source_snapshot(bundle: Path) -> dict[str, str]:
    """Hash immutable source evidence without following symlinks."""
    sources = bundle / "sources"
    snapshot: dict[str, str] = {}
    if not sources.is_dir():
        return snapshot
    for path in sorted(sources.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(bundle)
        if "inbox" in rel.parts or path.name == ".hashes.yaml":
            continue
        snapshot[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _source_policy_errors(
    bundle: Path,
    before: dict[str, str],
    expected_sha: str | None,
) -> list[str]:
    """Historical evidence is immutable; one pass may only add its submitted bytes."""
    after = _source_snapshot(bundle)
    errors: list[str] = []
    for rel, digest in sorted(before.items()):
        if rel not in after:
            errors.append(f"{rel}: curation deleted immutable source evidence")
        elif after[rel] != digest:
            errors.append(f"{rel}: curation modified immutable source evidence")
    for rel in sorted(set(after) - set(before)):
        if not expected_sha or after[rel] != expected_sha:
            errors.append(f"{rel}: curation added source evidence unrelated to this ingest")
    return errors


@dataclass(frozen=True)
class _ConceptState:
    substantive_signature: str
    generated_signature: str
    generated_at: datetime | None
    generated_at_raw: str
    verified_events: frozenset[tuple[str, str]]


def _instant(value: object) -> datetime | None:
    """Parse a generated timestamp for before/after comparison; invalid fails closed."""
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


def _substantive_signature(frontmatter: dict, body: str) -> str:
    """Hash knowledge content while ignoring generation/verification bookkeeping."""
    substantive = dict(frontmatter)
    substantive.pop("generated", None)
    substantive.pop("verified", None)
    canonical = yaml.safe_dump(substantive, sort_keys=True, allow_unicode=True)
    return hashlib.sha256((canonical + "\n---\n" + body).encode()).hexdigest()


def _generated_at(frontmatter: dict) -> tuple[datetime | None, str]:
    generated = frontmatter.get("generated")
    raw = generated.get("at") if isinstance(generated, dict) else None
    return _instant(raw), str(raw or "")


def _generated_signature(frontmatter: dict) -> str:
    return yaml.safe_dump(frontmatter.get("generated"), sort_keys=True, allow_unicode=True)


def _concept_snapshot(bundle: Path) -> dict[str, _ConceptState]:
    """Substantive content and trust bookkeeping before the untrusted curation pass."""
    snapshot: dict[str, _ConceptState] = {}
    for path in sorted(bundle.rglob("*.md")):
        if not should_check(path, bundle):
            continue
        try:
            frontmatter, body = parse_doc(path)
        except (OSError, ValueError):
            continue
        events = frozenset(
            (str(event.get("by") or ""), str(event.get("at") or ""))
            for event in normalize_verified(frontmatter)
        )
        generated_at, generated_at_raw = _generated_at(frontmatter)
        snapshot[path.relative_to(bundle).as_posix()] = _ConceptState(
            substantive_signature=_substantive_signature(frontmatter, body),
            generated_signature=_generated_signature(frontmatter),
            generated_at=generated_at,
            generated_at_raw=generated_at_raw,
            verified_events=events,
        )
    return snapshot


def _curation_policy_errors(
    bundle: Path,
    before: dict[str, _ConceptState],
    max_generated_at: datetime | None = None,
    *,
    actor: str = CURATOR_ACTOR,
) -> list[str]:
    """Enforce the write boundary that validation alone cannot infer.

    ``actor`` is the writer this pass stamps as ``generated.by``; the Codex curator
    is the default.
    """
    errors: list[str] = []
    after: set[str] = set()
    for path in sorted(bundle.rglob("*.md")):
        if not should_check(path, bundle):
            continue
        rel = path.relative_to(bundle).as_posix()
        after.add(rel)
        try:
            frontmatter, body = parse_doc(path)
        except (OSError, ValueError):
            continue  # deterministic bundle validation reports the parser error
        current_events = {
            (str(event.get("by") or ""), str(event.get("at") or ""))
            for event in normalize_verified(frontmatter)
        }
        sources = frontmatter.get("sources")
        if isinstance(sources, list):
            for i, source in enumerate(sources):
                if not isinstance(source, dict):
                    continue
                resource = source.get("resource")
                if not isinstance(resource, str) or not _local_resource_candidate(resource):
                    continue
                resolved = _source_resource_rel(resource.strip(), rel)
                if resolved is None:
                    errors.append(f"{rel}: sources[{i}].resource escapes the bundle: {resource!r}")
                elif not (bundle / resolved).is_file():
                    errors.append(
                        f"{rel}: sources[{i}].resource does not resolve to a local file: "
                        f"{resource!r} -> {resolved!r}"
                    )
        if rel not in before:
            generated = frontmatter.get("generated")
            generated_by = generated.get("by") if isinstance(generated, dict) else None
            generated_at, generated_at_raw = _generated_at(frontmatter)
            if frontmatter.get("status") != "draft":
                errors.append(f"{rel}: new concepts must start status draft")
            if current_events:
                errors.append(f"{rel}: curation must not verify a new concept")
            if generated_by != actor:
                errors.append(f"{rel}: new concepts must set generated.by to {actor!r}")
            if (
                max_generated_at is not None
                and generated_at is not None
                and generated_at > max_generated_at
            ):
                errors.append(
                    f"{rel}: generated.at must not exceed trusted pass time "
                    f"{max_generated_at.isoformat()}; got {generated_at_raw!r}"
                )
        else:
            prior = before[rel]
            generated = frontmatter.get("generated")
            generated_by = generated.get("by") if isinstance(generated, dict) else None
            generated_at, generated_at_raw = _generated_at(frontmatter)
            generated_changed = _generated_signature(frontmatter) != prior.generated_signature
            if current_events != prior.verified_events:
                errors.append(f"{rel}: curation must preserve verification history unchanged")
            substantive_changed = (
                _substantive_signature(frontmatter, body) != prior.substantive_signature
            )
            if generated_changed and not substantive_changed:
                errors.append(
                    f"{rel}: curation must not change generated metadata without substantive changes"
                )
            if generated_changed and generated_by != actor:
                errors.append(
                    f"{rel}: changed generation must set generated.by to {actor!r}"
                )
            if (
                generated_changed
                and max_generated_at is not None
                and generated_at is not None
                and generated_at > max_generated_at
            ):
                errors.append(
                    f"{rel}: generated.at must not exceed trusted pass time "
                    f"{max_generated_at.isoformat()}; got {generated_at_raw!r}"
                )
            if substantive_changed:
                if generated_by != actor:
                    errors.append(
                        f"{rel}: substantive curation must set generated.by to {actor!r}"
                    )
                if (
                    prior.generated_at is None
                    or generated_at is None
                    or generated_at <= prior.generated_at
                ):
                    errors.append(
                        f"{rel}: substantive curation must advance generated.at strictly after "
                        f"{prior.generated_at_raw!r}; got {generated_at_raw!r}"
                    )
                if current_verified(frontmatter):
                    errors.append(
                        f"{rel}: substantive curation retained verification current for the new generation; "
                        "only an audit job may verify changed knowledge"
                    )
    for deleted in sorted(set(before) - after):
        errors.append(f"{deleted}: curation deleted a concept; deprecate it instead")
    return errors


def _curation_provenance_errors(
    bundle: Path,
    before: dict[str, _ConceptState],
    source_snapshot: str,
) -> list[str]:
    """Every concept authored by this pass must cite this pass's immutable evidence."""
    errors: list[str] = []
    for path in sorted(bundle.rglob("*.md")):
        if not should_check(path, bundle):
            continue
        rel = path.relative_to(bundle).as_posix()
        try:
            frontmatter, body = parse_doc(path)
        except (OSError, ValueError):
            continue
        prior = before.get(rel)
        if prior is not None and _substantive_signature(frontmatter, body) == prior.substantive_signature:
            continue
        cited: set[str] = set()
        sources = frontmatter.get("sources")
        if isinstance(sources, list):
            for source in sources:
                resource = source.get("resource") if isinstance(source, dict) else None
                if not isinstance(resource, str):
                    continue
                resolved = _source_resource_rel(resource.strip(), rel)
                if resolved is not None:
                    cited.add(resolved)
        if source_snapshot not in cited:
            errors.append(
                f"{rel}: changed concepts must cite current ingest snapshot {source_snapshot!r}"
            )
    return errors


def _local_resource_candidate(resource: str) -> bool:
    """Avoid treating scope descriptors as paths; fail closed on path-shaped resources."""
    raw = resource.strip()
    if not raw or "://" in raw:
        return False
    last = raw.rstrip("/").rsplit("/", 1)[-1]
    return raw.startswith(("/", "./", "../", "sources/")) or "." in last
