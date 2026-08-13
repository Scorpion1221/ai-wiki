#!/usr/bin/env python3
"""Enforce data-preservation invariants when an OKF v0.2 concept is updated.

The #1 update hazard: an LLM re-writing a concept clobbers provenance (drops a
source, tag, contradiction, or verification event) or silently summarizes the
body away. This makes updates safe via a deterministic step run AROUND the edit:

    1. snapshot  — BEFORE editing: back up the concept to .okf/history/<name>-<ts>.md
    2. (the agent edits the concept's prose)
    3. enforce   — AFTER editing: diff against the snapshot and enforce:
         * union tags, structured sources, contradictions, and verification events
           without dropping prior entries
         * lock identity fields (type, title) to prior values
           (--allow-retype permits a deliberate type/title change)
         * stamp generated.by / generated.at for the meaningful content edit;
           verification is never fabricated or refreshed
         * body-shrink guard: if the new body < 70% of prior, demote status ->
           draft and warn (catches summarize-away);
           --allow-shrink overrides

`enforce` HARD-FAILS (exit 2) if it cannot find a prior snapshot for an existing
concept — the invariants would otherwise be silent no-ops.

Deterministic, Python3 + PyYAML + stdlib only.

Usage:
    update_concept.py snapshot <bundle> <concept-rel-path>
    update_concept.py enforce  <bundle> <concept-rel-path> [--prior <path>] [--allow-retype] [--allow-shrink]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aiwiki.engine.document import has_symlink_component

DELIM = "---"
LIST_FIELDS = ["tags", "contradictions"]
LOCK_FIELDS = ["type", "title"]
LEGACY_FIELDS = ["timestamp", "last_verified_at"]
VALID_STATUSES = {"draft", "stable", "deprecated"}
DEFAULT_GENERATED_BY = "process:ai-wiki-update-concept"
ACTOR_RE = re.compile(r"^(?:human:[^\s:]+|process:[^\s:]+|[^\s:/]+/[^\s/]+)$")
HISTORY = ".okf/history"
KEEP_BACKUPS = 10
SHRINK_RATIO = 0.70


class _Loader(yaml.SafeLoader):
    pass


# Keep timestamps as plain strings — otherwise PyYAML parses `2026-06-17T00:00:00Z`
# into a datetime and re-dumps it as `2026-06-17 00:00:00+00:00`, churning the field.
_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class _Dumper(yaml.SafeDumper):
    pass


# Mirror the loader: don't treat date-like strings as timestamps, so we emit them
# plain (e.g. 2026-06-17T00:00:00Z) instead of quoting them — keeps diffs minimal.
_Dumper.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeDumper.yaml_implicit_resolvers.items()
}


def _repr_list(dumper, data):
    # Keep scalar lists inline (tags: [a, b]); block-style otherwise.
    flow = all(isinstance(x, (str, int, float, bool)) for x in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_Dumper.add_representer(list, _repr_list)


def _parse(text: str):
    """Return (frontmatter dict, body str). Raises if no frontmatter block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != DELIM:
        raise ValueError("no YAML frontmatter")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == DELIM:
            end = i
            break
    if end is None:
        raise ValueError("unterminated frontmatter")
    fm = yaml.load("\n".join(lines[1:end]), Loader=_Loader) or {}
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a mapping")
    body = "\n".join(lines[end + 1:])
    return fm, body


def _serialize(fm: dict, body: str) -> str:
    dumped = yaml.dump(fm, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=4096)
    out = f"{DELIM}\n{dumped}{DELIM}\n{body}"
    return out if out.endswith("\n") else out + "\n"


def _union(cur, prior, field: str):
    if cur is not None and not isinstance(cur, list):
        raise ValueError(f"{field} must be a list")
    if prior is not None and not isinstance(prior, list):
        raise ValueError(f"{field} must be a list")
    cur = cur or []
    prior = prior or []
    out, seen = [], set()
    for x in list(cur) + list(prior):
        k = str(x)  # exact, case-sensitive — never collapse case-distinct ids/paths
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _validate_sources(value, *, where: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{where}.sources must be a list of mappings")
    out = []
    ids: dict[str, str] = {}
    for i, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(
                f"{where}.sources[{i}] must be a mapping with resource; "
                "legacy string sources are not supported"
            )
        resource = raw.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            raise ValueError(f"{where}.sources[{i}].resource must be a non-empty string")
        source = dict(raw)
        source["resource"] = resource.strip()
        source_id = source.get("id")
        if source_id is not None:
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError(f"{where}.sources[{i}].id must be a non-empty string")
            source_id = source_id.strip()
            if source_id in ids and ids[source_id] != source["resource"]:
                raise ValueError(
                    f"{where}.sources has duplicate id {source_id!r} for different resources"
                )
            ids[source_id] = source["resource"]
            source["id"] = source_id
        out.append(source)
    return out


def _merge_sources(current, prior) -> list[dict]:
    """Keep current ordering/values while restoring prior provenance and metadata."""
    cur = _validate_sources(current, where="current")
    old = _validate_sources(prior, where="prior")
    out = [dict(source) for source in cur]
    by_resource = {source["resource"]: i for i, source in enumerate(out)}
    for source in old:
        resource = source["resource"]
        if resource not in by_resource:
            by_resource[resource] = len(out)
            out.append(dict(source))
            continue
        i = by_resource[resource]
        # Current values win, but an edit cannot accidentally erase optional metadata.
        out[i] = {**source, **out[i]}
    # Re-run validation after merging to catch one id pointing at two resources.
    return _validate_sources(out, where="merged")


def _validate_verified(value, *, where: str) -> list[dict]:
    if value is None:
        return []
    events = [value] if isinstance(value, dict) else value
    if not isinstance(events, list):
        raise ValueError(f"{where}.verified must be a mapping or list of mappings")
    out = []
    for i, raw in enumerate(events):
        if not isinstance(raw, dict):
            raise ValueError(f"{where}.verified[{i}] must be a mapping")
        actor, at = raw.get("by"), raw.get("at")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError(f"{where}.verified[{i}].by must be a non-empty string")
        if not ACTOR_RE.fullmatch(actor.strip()):
            raise ValueError(f"{where}.verified[{i}].by is not a valid OKF actor: {actor!r}")
        if not isinstance(at, str) or not at.strip():
            raise ValueError(f"{where}.verified[{i}].at must be a non-empty ISO 8601 datetime")
        out.append(dict(raw))
    return out


def _merge_verified(current, prior) -> list[dict]:
    cur = _validate_verified(current, where="current")
    old = _validate_verified(prior, where="prior")
    out, seen = [], set()
    for event in cur + old:
        key = (event.get("by"), event.get("at"))
        if key not in seen:
            seen.add(key)
            out.append(event)
    return out


def _validate_v02(fm: dict, body: str, *, where: str) -> None:
    for field in LEGACY_FIELDS:
        if field in fm:
            raise ValueError(f"{where}: legacy field {field!r} is not supported by OKF v0.2")
    status = fm.get("status")
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(
            f"{where}: status must be one of {sorted(VALID_STATUSES)}; got {status!r}"
        )
    _validate_sources(fm.get("sources"), where=where)
    _validate_verified(fm.get("verified"), where=where)
    generated = fm.get("generated")
    if generated is not None:
        if not isinstance(generated, dict):
            raise ValueError(f"{where}.generated must be a mapping")
        actor = generated.get("by")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError(f"{where}.generated.by must be a non-empty string")
        if not ACTOR_RE.fullmatch(actor.strip()):
            raise ValueError(f"{where}.generated.by is not a valid OKF actor: {actor!r}")
    if any(line.strip() == "# Citations" for line in body.splitlines()):
        raise ValueError(
            f"{where}: legacy '# Citations' section is not supported; "
            "use sources objects and source-id footnotes"
        )


def _generated_now(actor: str) -> dict[str, str]:
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {"by": actor, "at": now}


def _history_paths(root: Path, rel: str):
    name = rel.replace("/", "__")
    hdir = root / HISTORY
    return hdir, name, sorted(hdir.glob(f"{name}-*.md")) if hdir.exists() else []


def _prune(root: Path, rel: str):
    _, _, backups = _history_paths(root, rel)
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink()


def cmd_snapshot(root: Path, rel: str) -> int:
    src = root / rel
    history_dir = root / HISTORY
    if has_symlink_component(root, src):
        print(f"error: unsafe concept path (outside bundle or symlink): {rel}", file=sys.stderr)
        return 2
    if has_symlink_component(root, history_dir):
        print(f"error: unsafe history path (outside bundle or symlink): {HISTORY}", file=sys.stderr)
        return 2
    if not src.is_file():
        print(f"error: concept not found: {rel}", file=sys.stderr)
        return 2
    text = src.read_text(encoding="utf-8")
    try:
        fm, body = _parse(text)
        _validate_v02(fm, body, where=rel)
    except (ValueError, yaml.YAMLError) as exc:
        print(f"error: invalid OKF v0.2 concept {rel}: {exc}", file=sys.stderr)
        return 2
    hdir, name, _ = _history_paths(root, rel)
    hdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S_%f")  # microsecond → unique within a second
    dest = hdir / f"{name}-{ts}.md"
    dest.write_text(text, encoding="utf-8")
    _prune(root, rel)
    print(f"snapshot → {dest.relative_to(root)}")
    return 0


def cmd_enforce(
    root: Path,
    rel: str,
    prior_path,
    allow_retype,
    allow_shrink,
    generated_by: str = DEFAULT_GENERATED_BY,
) -> int:
    cur_file = root / rel
    if has_symlink_component(root, cur_file):
        print(f"error: unsafe concept path (outside bundle or symlink): {rel}", file=sys.stderr)
        return 2
    if not cur_file.is_file():
        print(f"error: concept not found: {rel}", file=sys.stderr)
        return 2

    if prior_path:
        prior_file = Path(prior_path).expanduser()
    else:
        if has_symlink_component(root, root / HISTORY):
            print(f"error: unsafe history path (outside bundle or symlink): {HISTORY}", file=sys.stderr)
            return 2
        _, _, backups = _history_paths(root, rel)
        prior_file = backups[-1] if backups else None
    if not prior_file or not prior_file.is_file():
        print(f"error: no prior snapshot for {rel}. Run "
              f"`update_concept.py snapshot <bundle> {rel}` BEFORE editing.",
              file=sys.stderr)
        return 2

    try:
        pfm, pbody = _parse(prior_file.read_text(encoding="utf-8"))
        cfm, cbody = _parse(cur_file.read_text(encoding="utf-8"))
        _validate_v02(pfm, pbody, where=f"prior snapshot {prior_file.name}")
        _validate_v02(cfm, cbody, where=f"current concept {rel}")
    except (ValueError, yaml.YAMLError) as exc:
        print(f"error: invalid OKF v0.2 input: {exc}", file=sys.stderr)
        return 2
    notes = []

    # 1. shrink metric (on the agent's raw body, before metadata restoration)
    shrank = len(pbody) > 0 and len(cbody) < SHRINK_RATIO * len(pbody)

    # 2. scalar list unions
    for f in LIST_FIELDS:
        if f in pfm or f in cfm:
            try:
                merged = _union(cfm.get(f), pfm.get(f), f)
            except ValueError as exc:
                print(f"error: invalid OKF v0.2 input: {exc}", file=sys.stderr)
                return 2
            if merged:
                before = cfm.get(f)
                cfm[f] = merged
                if before != merged:
                    notes.append(f"union {f}: {before!r} + prior → {merged!r}")

    # 3. structured provenance and independent verification events
    try:
        sources = _merge_sources(cfm.get("sources"), pfm.get("sources"))
        verified = _merge_verified(cfm.get("verified"), pfm.get("verified"))
    except ValueError as exc:
        print(f"error: invalid OKF v0.2 input: {exc}", file=sys.stderr)
        return 2
    if sources:
        if sources != cfm.get("sources"):
            notes.append("restored dropped source provenance or metadata")
        cfm["sources"] = sources
    if verified:
        if verified != _validate_verified(cfm.get("verified"), where="current"):
            notes.append("restored dropped verification event(s)")
        cfm["verified"] = verified

    # 4. lock identity fields
    for f in LOCK_FIELDS:
        if f in pfm and cfm.get(f) != pfm[f]:
            if allow_retype and f in ("type", "title"):
                notes.append(f"RETYPE allowed: {f} {pfm[f]!r} → {cfm.get(f)!r} "
                             f"(remember to move the file / rewrite inbound links)")
            else:
                notes.append(f"locked {f}: restored {pfm[f]!r} (was {cfm.get(f)!r})")
                cfm[f] = pfm[f]

    # 5. record the edit. Verification is deliberately left untouched.
    if not isinstance(generated_by, str) or not ACTOR_RE.fullmatch(generated_by.strip()):
        print(
            "error: --generated-by must use human:<id>, process:<id>, or <producer>/<version>",
            file=sys.stderr,
        )
        return 2
    cfm["generated"] = _generated_now(generated_by.strip())
    notes.append(f"updated generated.by/at for {generated_by.strip()}")

    # 6. body-shrink guard
    if shrank and not allow_shrink:
        if cfm.get("status") != "draft":
            notes.append(f"BODY SHRANK >{int((1-SHRINK_RATIO)*100)}% "
                         f"→ status demoted to draft (was {cfm.get('status')!r}; --allow-shrink to keep)")
            cfm["status"] = "draft"
        else:
            notes.append("body shrank >30% (status already draft)")

    cur_file.write_text(_serialize(cfm, cbody), encoding="utf-8")
    print(f"enforced invariants on {rel} (prior: {prior_file.name})")
    for n in notes:
        print(f"  • {n}")
    if not notes:
        print("  • no changes needed (already consistent)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Enforce update invariants on an OKF concept.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="back up a concept BEFORE editing")
    s.add_argument("bundle", type=Path)
    s.add_argument("concept")
    e = sub.add_parser("enforce", help="enforce invariants AFTER editing")
    e.add_argument("bundle", type=Path)
    e.add_argument("concept")
    e.add_argument("--prior", default=None, help="prior snapshot path (default: latest in .okf/history)")
    e.add_argument("--allow-retype", action="store_true")
    e.add_argument("--allow-shrink", action="store_true")
    e.add_argument(
        "--generated-by",
        default=DEFAULT_GENERATED_BY,
        help=f"OKF actor recorded in generated.by (default: {DEFAULT_GENERATED_BY})",
    )
    a = ap.parse_args(argv)

    root = a.bundle.expanduser().resolve()
    if not root.is_dir():
        ap.error(f"not a bundle directory: {root}")
    rel = a.concept.replace("\\", "/").strip("/")

    if a.cmd == "snapshot":
        return cmd_snapshot(root, rel)
    return cmd_enforce(
        root,
        rel,
        a.prior,
        a.allow_retype,
        a.allow_shrink,
        a.generated_by,
    )


if __name__ == "__main__":
    raise SystemExit(main())
