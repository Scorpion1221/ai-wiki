"""Service-owned OKF bookkeeping for agent passes and proposed changesets.

An editor (a curation or audit agent, or a future changeset) decides knowledge content
only. The service owns the bookkeeping keys: it restores them from the pre-edit
document, then stamps them deterministically with trusted time.

* ``generated`` becomes ``{by: actor, at: trusted time}`` only when content changed
  substantively (whitespace, key order and scalar quoting do not count).
* ``verified`` history is never edited by the editor; an audit verdict of
  ``verified`` appends one event.
* audit also freezes ``sources`` and owns ``status`` (``draft`` becomes ``stable``).
* a curate changeset owns ``status`` too: a new concept starts ``draft``, an existing
  one keeps its value, and a deprecate operation sets ``deprecated``.

Patching is key-level text surgery, so every untouched key keeps its YAML style.
Leading body lines that are spilled frontmatter or verification events are removed.
The module is pure (text in, text out) and runs no agent.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

from .document import OKFDocumentError, _instant, normalize_verified, parse_document
from .scan_sources import _source_resource_rel
from .validate import SPILL_KEY_RE, split_body_spill

# Keys the service owns at each stage. They never count as knowledge content.
SERVICE_KEYS = {
    "curate": ("verified", "generated"),
    "audit": ("verified", "generated", "status", "sources"),
    "changeset": ("verified", "generated", "status"),
}
VERDICTS = {"verified", "unverified"}
# Verification-event fields an editor may drop to column 0 (a lost ``- ``): never content.
_EVENT_FIELDS = ("by", "at")
_CANONICAL_ORDER = ["type", "title", "description", "tags", "status", "generated", "sources"]
_RESTORED = {
    "curate": {"verified": "restored service-owned verification history without adding verification"},
    "audit": {
        "verified": "restored service-owned verification history",
        "status": "restored service-owned status",
        "sources": "restored immutable sources provenance",
    },
    "changeset": {
        "verified": "restored service-owned verification history without adding verification",
        "status": "restored service-owned status",
    },
}
# A top-level YAML key at column 0; indented, list, comment and blank lines belong to it.
_KEY = re.compile(
    r"""^(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<plain>[^\s#'"?{}\[\],&*!|>%@`-][^:#]*?))[ \t]*:(?:[ \t\r]|$)"""
)
_ABSENT = object()
_INVALID = object()
_MERGE_TAG = "tag:yaml.org,2002:merge"

Block = tuple[str | None, list[str]]


class BookkeepingError(ValueError):
    """The edited document cannot be patched into a parseable OKF concept."""

    def __init__(self, message: str, *, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line  # 1-based line in the edited file, when known


class _TextLoader(yaml.SafeLoader):
    """Load timestamps as written, so re-quoting a date is not a content change."""


class _TextDumper(yaml.SafeDumper):
    pass


_TextLoader.yaml_implicit_resolvers = {
    key: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_TextDumper.yaml_implicit_resolvers = _TextLoader.yaml_implicit_resolvers


def _split(text: str) -> tuple[list[str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise BookkeepingError("missing YAML frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise BookkeepingError("unterminated YAML frontmatter")
    frontmatter = [line if line.endswith("\n") else line + "\n" for line in lines[1:end]]
    return frontmatter, "".join(lines[end + 1:])


def _blocks(lines: list[str]) -> list[Block]:
    blocks: list[Block] = []
    for line in lines:
        match = _KEY.match(line)
        if match:
            key = next(group for group in match.group("dq", "sq", "plain") if group is not None)
            blocks.append((key.strip(), [line]))
        elif blocks:
            blocks[-1][1].append(line)
        else:
            blocks.append((None, [line]))
    return blocks


def _require_plain_keys(frontmatter: list[str]) -> None:
    """Refuse a top-level key that YAML reads other than as its block is named.

    Surgery finds keys by their written names, so an escaped (``"stat\\x75s"``), tagged,
    explicit (``? status``) or merge (``<<``) key, or one hidden in another key's block,
    would carry a value past it.
    """
    try:
        root = yaml.compose("".join(frontmatter), Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return  # a syntax error is reported where the document is parsed
    pairs = root.value if isinstance(root, yaml.MappingNode) else []
    read = [key.value if isinstance(key, yaml.ScalarNode) and key.tag != _MERGE_TAG else None for key, _ in pairs]
    written, line = [], 2  # the frontmatter starts on file line 2
    for key, lines in _blocks(frontmatter):
        if key is not None:
            written.append((key, line))
        line += len(lines)
    for index in range(max(len(read), len(written))):
        name = read[index] if index < len(read) else None
        if index >= len(written) or name != written[index][0]:
            at = pairs[index][0].start_mark.line + 2 if index < len(pairs) else written[index][1]
            shown = name if name is not None else written[index][0]
            raise BookkeepingError(f"frontmatter key {shown!r} is not written as a plain name", line=at)


def _join(blocks: list[Block]) -> str:
    return "".join(line for _key, lines in blocks for line in lines)


def _lines(blocks: list[Block], key: str) -> list[str] | None:
    """Like a YAML loader, the last duplicate key wins."""
    found = None
    for block_key, lines in blocks:
        if block_key == key:
            found = lines
    return found


def _value(lines: list[str] | None) -> Any:
    if lines is None:
        return _ABSENT
    try:
        loaded = yaml.load("".join(lines), Loader=_TextLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError:
        return _INVALID
    return next(iter(loaded.values()), None) if isinstance(loaded, dict) else _INVALID


def _put(blocks: list[Block], key: str, lines: list[str] | None, order: list[str]) -> list[Block]:
    """Replace every ``key`` block by one (or none); insert a missing key near its peers."""
    result: list[Block] = []
    placed = False
    for block_key, block_lines in blocks:
        if block_key != key:
            result.append((block_key, block_lines))
        elif lines is not None and not placed:
            result.append((key, lines))
            placed = True
    if lines is None or placed:
        return result
    sequence = order if key in order else _CANONICAL_ORDER
    if key not in sequence:
        return result + [(key, lines)]
    present = [block_key for block_key, _lines in result]
    position = sequence.index(key)
    for anchor in reversed(sequence[:position]):
        if anchor in present:
            index = len(present) - present[::-1].index(anchor)
            return result[:index] + [(key, lines)] + result[index:]
    for anchor in sequence[position + 1:]:
        if anchor in present:
            index = present.index(anchor)
            return result[:index] + [(key, lines)] + result[index:]
    return result + [(key, lines)]


def _inline(head: str) -> bool:
    value = head.split(":", 1)[1].strip() if ":" in head else ""
    return bool(value) and not value.startswith("#")


def _like(template: list[str], field: str, value: str) -> str:
    """Quote a new scalar the way the template quoted the same field."""
    match = re.search(rf"\b{field}\s*:\s*(['\"])", "".join(template))
    return f"{match.group(1)}{value}{match.group(1)}" if match else value


def _generated_lines(template: list[str] | None, actor: str, at: str) -> list[str]:
    template = template or []
    if template and not _inline(template[0]):
        child = next((line for line in template[1:] if line.strip()), "  by:\n")
        pad = child[: len(child) - len(child.lstrip())] or "  "
        return [
            "generated:\n",
            f"{pad}by: {_like(template, 'by', actor)}\n",
            f"{pad}at: {_like(template, 'at', at)}\n",
        ]
    return [f"generated: {{by: {_like(template, 'by', actor)}, at: {_like(template, 'at', at)}}}\n"]


def _with_event(template: list[str] | None, actor: str, at: str) -> list[str]:
    """Append one verification event, matching the list's indentation and item style."""
    if not template:
        return ["verified:\n", f"  - {{by: {actor}, at: {at}}}\n"]
    lines = list(template)
    tail: list[str] = []
    while len(lines) > 1 and not lines[-1].strip():
        tail.insert(0, lines.pop())
    items = [line for line in lines[1:] if line.strip() and not line.lstrip().startswith("#")]
    if not _inline(lines[0]) and items and items[0].lstrip().startswith("-"):
        pad = items[0][: len(items[0]) - len(items[0].lstrip())]
        last = lines[[i for i, line in enumerate(lines) if line.startswith(pad + "-")][-1]:]
        if last[0].lstrip()[1:].lstrip().startswith("{"):
            new = [f"{pad}- {{by: {_like(last, 'by', actor)}, at: {_like(last, 'at', at)}}}\n"]
        else:
            child = next((line for line in last[1:] if line.strip()), pad + "  at:\n")
            child_pad = child[: len(child) - len(child.lstrip())]
            new = [f"{pad}- by: {_like(last, 'by', actor)}\n", f"{child_pad}at: {_like(last, 'at', at)}\n"]
        return lines + new + tail
    # A single mapping or an inline value: rewrite only this key, as a list.
    value = _value(template)
    events = normalize_verified({"verified": None if value is _INVALID else value})
    events.append({"by": actor, "at": at})
    dumped = [
        yaml.dump(event, Dumper=_TextDumper, default_flow_style=True, sort_keys=False,
                  allow_unicode=True, width=4096).strip()
        for event in events
    ]
    return ["verified:\n", *(f"  - {event}\n" for event in dumped), *tail]


def _normalize_body(body: str) -> str:
    """Trailing spaces, blank-line runs and spilled bookkeeping are not content."""
    _spilled, body = split_body_spill(body)
    normalized: list[str] = []
    for line in (line.rstrip() for line in body.splitlines()):
        if line or (normalized and normalized[-1]):
            normalized.append(line)
    return "\n".join(normalized).strip("\n")


def _signature(frontmatter: str, body: str, stage: str) -> str:
    try:
        loaded = yaml.load(frontmatter, Loader=_TextLoader) or {}  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise BookkeepingError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(loaded, dict):
        raise BookkeepingError("frontmatter must be a mapping")
    content = {key: value for key, value in loaded.items() if key not in SERVICE_KEYS[stage]}
    return json.dumps(content, sort_keys=True, ensure_ascii=False, default=str) + "\n---\n" + _normalize_body(body)


def substantive_change(before_text: str | None, after_text: str, *, stage: str) -> bool:
    """Whether an edit changed knowledge content, ignoring this stage's service keys."""
    if before_text is None:
        return True
    before_fm, before_body = _split(before_text)
    after_fm, after_body = _split(after_text)
    return _signature("".join(before_fm), before_body, stage) != _signature(
        "".join(after_fm), after_body, stage,
    )


def _distance(a: str, b: str, limit: int) -> int | None:
    """Levenshtein distance when it is at most ``limit`` (banded), else None."""
    if abs(len(a) - len(b)) > limit:
        return None
    worst = limit + 1
    previous = [j if j <= limit else worst for j in range(len(b) + 1)]
    for i in range(1, len(a) + 1):
        current = [i if i <= limit else worst] + [worst] * len(b)
        for j in range(max(1, i - limit), min(len(b), i + limit) + 1):
            current[j] = min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a[i - 1] != b[j - 1]), worst,
            )
        if min(current) > limit:
            return None
        previous = current
    return previous[-1] if previous[-1] <= limit else None


def normalize_snapshot_reference(
    text: str,
    *,
    concept_rel: str,
    snapshot_rel: str,
    existing: set[str],
) -> tuple[str, list[str]]:
    """Point one mistyped reference at the current immutable snapshot when unambiguous.

    Editors copy long content-addressed snapshot names by hand and drop or swap
    characters, or cite them relative to the wrong directory. A reference is
    rewritten to ``/<snapshot_rel>`` only when the concept does not already cite the
    snapshot, exactly one of its sources fails to resolve to an ``existing`` file,
    and that file name is within a small edit distance of the snapshot name and
    strictly closer to it than to any other existing source.
    """
    try:
        sources = parse_document(text).frontmatter.get("sources")
    except OKFDocumentError:
        return text, []
    if not isinstance(sources, list):
        return text, []
    resources = [
        (index, source["resource"].strip())
        for index, source in enumerate(sources)
        if isinstance(source, dict) and isinstance(source.get("resource"), str)
    ]
    resolved = {index: _source_resource_rel(resource, concept_rel) for index, resource in resources}
    if snapshot_rel in resolved.values():
        return text, []
    unresolved = [
        (index, resource) for index, resource in resources
        if resolved[index] is not None and resolved[index] not in existing
    ]
    target = snapshot_rel.rsplit("/", 1)[-1]
    limit = max(1, min(8, len(target) // 16))
    near = [
        (index, resource, distance)
        for index, resource in unresolved
        if (distance := _distance(resolved[index].rsplit("/", 1)[-1], target, limit)) is not None
    ]
    if len(near) != 1:
        return text, []
    index, resource, distance = near[0]
    name = resolved[index].rsplit("/", 1)[-1]
    if any(
        _distance(name, other.rsplit("/", 1)[-1], distance) is not None
        for other in existing if other != snapshot_rel
    ):
        return text, []
    frontmatter, body = _split(text)
    blocks = _blocks(frontmatter)
    lines = _lines(blocks, "sources") or []
    if sum(line.count(resource) for line in lines) != 1:
        return text, []
    replacement = "/" + snapshot_rel
    patched = [line.replace(resource, replacement) for line in lines]
    candidate = "---\n" + _join(_put(blocks, "sources", patched, [])) + "---\n" + body
    try:
        parse_document(candidate)
    except OKFDocumentError:
        return text, []
    return candidate, [
        f"normalized sources[{index}].resource to the current snapshot: {resource!r} -> {replacement!r}"
    ]


def append_sources(text: str, entries: list[dict]) -> str:
    """Append source entries to ``sources``, matching a block list's indentation.

    Existing items keep their bytes. Each new entry is one flow mapping. A missing or
    inline ``sources`` value is rewritten as a block list of its entries plus these.
    """
    if not entries:
        return text
    frontmatter, body = _split(text)
    blocks = _blocks(frontmatter)
    lines = _lines(blocks, "sources")
    flow = [
        yaml.safe_dump(entry, default_flow_style=True, sort_keys=False, allow_unicode=True, width=4096).strip()
        for entry in entries
    ]
    items = [line for line in (lines or [])[1:] if line.strip() and not line.lstrip().startswith("#")]
    if lines and not _inline(lines[0]) and items and items[0].lstrip().startswith("-"):
        pad = items[0][: len(items[0]) - len(items[0].lstrip())]
        patched = list(lines)
        tail: list[str] = []
        while len(patched) > 1 and not patched[-1].strip():
            tail.insert(0, patched.pop())
        patched += [f"{pad}- {item}\n" for item in flow] + tail
    else:
        current = _value(lines)
        kept = [
            yaml.safe_dump(entry, default_flow_style=True, sort_keys=False, allow_unicode=True, width=4096).strip()
            for entry in (current if isinstance(current, list) else [])
        ]
        patched = ["sources:\n", *(f"  - {item}\n" for item in kept + flow)]
    return "---\n" + _join(_put(blocks, "sources", patched, [])) + "---\n" + body


def rewrite_source_resource(text: str, *, placeholder: str, resource: str) -> tuple[str, list[int]]:
    """Replace every ``sources[].resource`` equal to ``placeholder`` with ``resource``.

    Returns the text and the rewritten indices. The text is unchanged (and no index is
    returned) when the placeholder also appears elsewhere in ``sources``, so a rewrite
    never touches a title or reference that merely quotes it.
    """
    try:
        sources = parse_document(text).frontmatter.get("sources")
    except OKFDocumentError:
        return text, []
    indices = [
        index for index, source in enumerate(sources if isinstance(sources, list) else [])
        if isinstance(source, dict) and isinstance(source.get("resource"), str)
        and source["resource"].strip() == placeholder
    ]
    frontmatter, body = _split(text)
    blocks = _blocks(frontmatter)
    lines = _lines(blocks, "sources") or []
    if not indices or sum(line.count(placeholder) for line in lines) != len(indices):
        return text, []
    patched = [line.replace(placeholder, resource) for line in lines]
    candidate = "---\n" + _join(_put(blocks, "sources", patched, [])) + "---\n" + body
    try:
        rewritten = parse_document(candidate).frontmatter.get("sources")
    except OKFDocumentError:
        return text, []
    if not isinstance(rewritten, list) or any(rewritten[index].get("resource") != resource for index in indices):
        return text, []
    return candidate, indices


def _seconds(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


def _not_before(trusted_now: datetime, floor: datetime | None, *, strictly: bool = False) -> datetime:
    """Trusted time, moved past a recorded (possibly skewed) event when necessary."""
    stamp = _seconds(trusted_now)
    if floor is not None and (stamp <= floor if strictly else stamp < floor):
        stamp = _seconds(floor)
        if stamp < floor or strictly:
            stamp += timedelta(seconds=1)
    return stamp


def _text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_bookkeeping(
    before_text: str | None,
    after_text: str,
    *,
    actor: str,
    trusted_now: datetime,
    stage: str,
    verdict: str | None = None,
    deprecate: bool = False,
) -> tuple[str, list[str]]:
    """Return the edited document with service-owned bookkeeping restored and stamped.

    ``before_text`` is the pre-edit document (None for a new concept; curation and
    changesets only). ``verdict`` is the audit outcome for this concept: ``verified``
    appends one ``{by: actor, at: trusted time}`` event; ``unverified`` adds none.
    ``deprecate`` marks a changeset deprecate operation, whose status is ``deprecated``.
    The second return value lists deterministic repairs (discarded editor bookkeeping,
    spill). Raises ``BookkeepingError`` when the edit cannot be made a parseable concept,
    or when a changeset edit writes a top-level key other than as a plain name.
    """
    if stage not in SERVICE_KEYS:
        raise ValueError(f"unknown bookkeeping stage {stage!r}")
    if verdict is not None and (stage != "audit" or verdict not in VERDICTS):
        raise ValueError("only an audit may pass a verified/unverified verdict")
    if before_text is None and (stage == "audit" or deprecate):
        raise ValueError("audit and deprecate bookkeeping require the pre-edit document")
    if deprecate and stage != "changeset":
        raise ValueError("only a changeset may deprecate a concept")
    if trusted_now.tzinfo is None:
        raise ValueError("trusted_now must be timezone-aware")

    after_fm, after_body = _split(after_text)
    if stage == "changeset":
        _require_plain_keys(after_fm)
    after_blocks = _blocks(after_fm)
    before_blocks: list[Block] = []
    before_body = ""
    before_doc: dict = {}
    if before_text is not None:
        before_fm, before_body = _split(before_text)
        before_blocks = _blocks(before_fm)
        try:
            before_doc = parse_document(before_text).frontmatter
        except OKFDocumentError as exc:
            raise BookkeepingError(f"pre-edit document is invalid: {exc}") from exc
    order = [key for key, _lines in (before_blocks or after_blocks) if key is not None]
    status = None
    if stage == "audit":
        status = "deprecated" if before_doc.get("status") == "deprecated" else "stable"
    elif stage == "changeset":
        status = "deprecated" if deprecate else "draft" if before_text is None else None

    repairs: list[str] = []
    restored = after_blocks
    for key in SERVICE_KEYS[stage]:
        before_lines = _lines(before_blocks, key)
        edited = _value(_lines(after_blocks, key))
        if key != "generated" and edited != _value(before_lines) and not (key == "status" and edited == status):
            repairs.append(_RESTORED[stage][key])
        restored = _put(restored, key, before_lines, order)
    before_keys = {key for key, _lines in before_blocks}
    for key, lines in restored:
        if key in _EVENT_FIELDS and key not in before_keys:
            repairs.append(f"removed spilled verification field from frontmatter: {''.join(lines).strip()!r}")
    restored = [(key, lines) for key, lines in restored if key not in _EVENT_FIELDS or key in before_keys]
    # Spill the editor added is discarded whichever body survives; say so in the receipt.
    editor_spill = Counter(split_body_spill(after_body)[0]) - Counter(split_body_spill(before_body)[0])
    repairs.extend(
        f"discarded spilled frontmatter line written by editor: {line.strip()!r}"
        for line in editor_spill.elements()
    )

    substantive = before_text is None or (
        _signature(_join(restored), after_body, stage) != _signature(_join(before_blocks), before_body, stage)
    )
    if substantive:
        blocks, body = restored, after_body
    else:
        if _value(_lines(after_blocks, "generated")) != _value(_lines(before_blocks, "generated")):
            repairs.append("restored generated after discarded non-substantive edits")
        elif _join(restored) != _join(before_blocks) or (
            split_body_spill(after_body)[1] != split_body_spill(before_body)[1]
        ):
            repairs.append("discarded non-substantive formatting edits")
        if stage != "audit" and not deprecate:
            return before_text, repairs
        blocks, body = before_blocks, before_body

    generated = before_doc.get("generated")
    generated_at = _instant(generated.get("at")) if isinstance(generated, dict) else None
    stamp = None
    if substantive:
        # A new generation supersedes every earlier generation and verification event.
        history = [_instant(event.get("at")) for event in normalize_verified(before_doc)]
        stamp = _not_before(trusted_now, max(filter(None, [generated_at, *history]), default=None), strictly=True)
        template = _lines(before_blocks, "generated") or _lines(after_blocks, "generated")
        blocks = _put(blocks, "generated", _generated_lines(template, actor, _text(stamp)), order)
    if status is not None and _value(_lines(blocks, "status")) != status:
        blocks = _put(blocks, "status", [f"status: {status}\n"], order)
    if verdict == "verified":
        # The event confirms the current generation, so it may never predate it.
        event_at = stamp or _not_before(trusted_now, generated_at)
        blocks = _put(blocks, "verified", _with_event(_lines(blocks, "verified"), actor, _text(event_at)), order)

    spilled, body = split_body_spill(body)
    present = {key for key, _lines in blocks}
    displaced = sorted({
        match.group(1) for line in spilled if (match := SPILL_KEY_RE.match(line))
    } - set(SERVICE_KEYS[stage]) - present)
    if displaced:
        # Knowledge fields, not bookkeeping: dropping them would lose content silently.
        raise BookkeepingError(f"frontmatter key(s) spilled into the body: {', '.join(displaced)}")
    if not substantive:
        editor_spill = Counter()  # the editor's body was discarded; its spill is already reported
    for line in spilled:
        if editor_spill[line]:
            editor_spill[line] -= 1
        else:
            repairs.append(f"removed spilled frontmatter line from body: {line.strip()!r}")
    text = "---\n" + _join(blocks) + "---\n" + body
    try:
        parse_document(text)
    except OKFDocumentError as exc:
        raise BookkeepingError(str(exc)) from exc
    return text, repairs
