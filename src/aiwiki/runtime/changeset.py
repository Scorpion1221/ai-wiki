"""Curate changesets: the deterministic gate for proposed concept edits (design §2.2–§2.9).

A changeset carries whole concept files and names one evidence packet, assembled here
from frozen bytes. The gate runs no agent and no subprocess: it checks the request,
stamps service-owned bookkeeping, and judges the result with the same policy and
validation as the Codex path. ``evaluate`` is pure (it works on a disposable copy of
``base_dir``), so the service dry-run, the writer transaction and a local
``ai-wiki validate`` reach one verdict with one error format.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import posixpath
import re
import shutil
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from ..engine import bookkeeping
from ..engine.document import OKFDocumentError, has_symlink_component, parse_document
from ..engine.lint import lint
from ..engine.update_concept import SHRINK_RATIO
from ..engine.validate import OKF_RESERVED, PROFILE_STRUCTURAL, parse_doc, should_check, validate, validate_changed
from ..service.ingest import MAX_BYTES, write_source
from . import policy, secrets
from .failure import failure

SCHEMA = "ai-wiki.changeset/v1"
PACKET_RESOURCE = "evidence:packet"
HASH_PREFIX = "ch1:"
# Keys the service stamps. They never count toward a concept's content hash.
SERVICE_KEYS = frozenset(bookkeeping.SERVICE_KEYS["changeset"])
VERDICTS = ("verified", "corrected", "unverified")

# name -> (environment variable, default)
_LIMITS = {
    "files": ("AIWIKI_CHANGESET_MAX_FILES", 20),
    "concept_bytes": ("AIWIKI_CHANGESET_MAX_CONCEPT_BYTES", 128 * 1024),
    "packet_text_bytes": ("AIWIKI_CHANGESET_MAX_PACKET_TEXT_BYTES", 1024 * 1024),
    "packet_binary_bytes": ("AIWIKI_CHANGESET_MAX_PACKET_BINARY_BYTES", MAX_BYTES),
    "deprecates": ("AIWIKI_CHANGESET_MAX_DEPRECATES", 3),
    "reviews": ("AIWIKI_CHANGESET_MAX_REVIEWS", 5),
}
# Error code -> HTTP status. The failure class follows from the status (see _failure).
CODES = {
    "input": 400, "too_large": 413,
    "conflict": 409, "unknown_base": 409, "work_item_closed": 409, "lease_required": 409,
    "yaml_parse": 422, "missing_key": 422, "invalid_value": 422, "legacy_key": 422,
    "invalid_status": 422, "uncited_change": 422, "unknown_evidence_ref": 422,
    "resource_unresolvable": 422, "broken_link": 422, "dangling_contradiction": 422,
    "duplicate_title": 422, "body_shrink": 422, "identity_locked": 422, "path_forbidden": 422,
    "service_owned_path": 422, "delete_forbidden": 422, "restructure_new_source": 422,
    "secret_detected": 422, "validation": 422,
}
HINTS = {
    "missing_key": "add the key to the frontmatter",
    "invalid_value": "give the field the documented shape",
    "legacy_key": "remove the legacy field; OKF v0.2 does not accept it",
    "invalid_status": "omit status; the service sets it",
    "uncited_change": "cite {id: <evidence.id>, resource: evidence:packet} or revert the file",
    "unknown_evidence_ref": "cite this changeset's packet as {id: <evidence.id>, resource: evidence:packet}",
    "resource_unresolvable": "cite evidence:packet or an existing /sources/ file",
    "broken_link": "link an existing concept or one this changeset creates",
    "dangling_contradiction": "point contradictions at an existing concept",
    "duplicate_title": "update the existing concept instead of creating another",
    "body_shrink": "restore the removed content, or list the path in allow.shrink with a reason",
    "identity_locked": "restore type and title, or list the path in allow.retype with a reason",
    "path_forbidden": "remove this file from the changeset",
    "service_owned_path": "remove this file; the service writes it",
    "delete_forbidden": "deprecate the concept instead",
    "secret_detected": "remove the secret; this is parked, not retried",
    "conflict": "workspace pull, then re-apply the change to the current version",
    "validation": "fix the reported problem",
}
# Validator, policy and lint messages -> error code; the first match wins.
_MESSAGE_CODES = (
    (re.compile(r"YAML frontmatter|frontmatter must be a mapping|spilled"), "yaml_parse"),
    (re.compile(r"missing required frontmatter key"), "missing_key"),
    (re.compile(r"invalid status"), "invalid_status"),
    (re.compile(r"\blegacy\b"), "legacy_key"),
    (re.compile(r"does not resolve to a local file|resource escapes the bundle"), "resource_unresolvable"),
    (re.compile(r"must cite current ingest snapshot"), "uncited_change"),
    (re.compile(r"unresolved link|link escapes bundle|link traverses symlink"), "broken_link"),
    (re.compile(r"contradictions target"), "dangling_contradiction"),
    (re.compile(r"deleted a concept"), "delete_forbidden"),
    (re.compile(r"source evidence|more than one snapshot"), "service_owned_path"),
    (re.compile(r"symlink|prohibited non-concept"), "path_forbidden"),
    # Service-stamped bookkeeping: seeing one of these means the gate itself is wrong.
    (re.compile(r"\bgenerated\b|\bverified\b|verif(?:y|ication)|status draft"), "validation"),
    (re.compile(r" must | duplicates "), "invalid_value"),
)
_YAML_HINTS = (
    ("mapping values are not allowed", "quote the scalar containing ':'"),
    ("expected <block end>, but found '-'", "indent this list item like the items above it"),
    ("found character '\\t'", "indent with spaces, not tabs"),
    ("not written as a plain name", "write each top-level key once as a plain name: no escapes, tags, '?' or '<<'"),
    ("spilled into the body", "move these keys back above the closing ---"),
)
# PyYAML's scalar constructors raise these, not YAMLError, for values like 2026-02-30 or !!bool x.
_CONSTRUCTOR_ERRORS = (ValueError, AttributeError, LookupError, TypeError)
_DIGITS = re.compile(r"\d+")
_REVISION = re.compile(r"^[0-9a-f]{7,64}$")
_CONTENT_HASH = re.compile(r"^ch1:[0-9a-f]{64}$")
_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_WORK_ITEM = re.compile(r"^it_[A-Za-z0-9]{1,64}$")
# C0/C1 controls and line separators would forge lines wherever a path is logged (log.md).
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_FILE_NAME = r"(?!\.\.?$)[^/\\\x00-\x1f\x7f-\x9f\u2028\u2029]{1,200}"
_ITEM_FILE = re.compile(rf"^(it_[A-Za-z0-9]{{1,64}})/({_FILE_NAME})$")
_MAX_SEGMENT_BYTES, _MAX_PATH_BYTES = 200, 512
_SERVICE_OWNED_NAMES = OKF_RESERVED | PROFILE_STRUCTURAL | {"index-meta.yaml", "viz.html"}
_REMOVALS = {"delete", "remove", "rename", "move"}
_KEYS = {
    "curate": {"schema", "kind", "intent", "base_revision", "work_items", "close_items", "evidence",
               "files", "allow", "run", "message"},
    "audit": {"schema", "kind", "base_revision", "reviews", "run", "message"},
}


@dataclass(frozen=True)
class EvidenceFile:
    """A frozen work-item file named in ``evidence.item_files`` (``<item id>/<name>``)."""

    name: str
    data: bytes
    origin: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Packet:
    """The one evidence packet of a changeset. ``filename`` fixes the stored extension."""

    filename: str
    data: bytes
    parts: tuple[dict, ...]


def limits() -> dict[str, int]:
    """Gate limits; each can be tuned with its environment variable."""
    return {name: int(os.environ.get(env, default)) for name, (env, default) in _LIMITS.items()}


def _error(code: str, message: str, path: str | None = None, **extra) -> dict:
    error = {"code": code}
    if path is not None:
        error["path"] = path
    error.update(extra)
    error["message"] = message
    if code in HINTS and "hint" not in extra:
        error["hint"] = HINTS[code]
    return error


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_ws(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()).rstrip("\n") + "\n"


def content_hash(text: str) -> str:
    """CAS hash of a concept: content keys and whitespace-normalized body (§2.6).

    ``generated``, ``verified`` and ``status`` are excluded, so an audit stamp never
    invalidates a curator's base while any content edit does. A document whose
    frontmatter does not parse is hashed as raw text, so it can still be compared.
    """
    try:
        document = parse_document(text)
    except OKFDocumentError:
        payload: dict = {"raw": _normalize_ws(text)}
    else:
        frontmatter = {str(key): value for key, value in document.frontmatter.items()}
        payload = {
            "fm": {key: frontmatter[key] for key in sorted(frontmatter) if key not in SERVICE_KEYS},
            "body": _normalize_ws(document.body),
        }
    return HASH_PREFIX + _sha(_canonical(payload).encode("utf-8"))


def _upload_bytes(upload: Mapping) -> bytes:
    return base64.b64decode(str(upload.get("content_b64") or ""), validate=True)


def changeset_sha256(request: Mapping, evidence_sha256: Mapping[str, str] | None = None) -> str:
    """Idempotency key (§2.7): content and evidence digests, never base, run or message.

    ``evidence_sha256`` maps each ``evidence.item_files`` name to its frozen bytes' sha256.
    Call it only for a request ``check_request`` admitted.
    """
    evidence = request.get("evidence") if isinstance(request.get("evidence"), Mapping) else None
    files = []
    for entry in request.get("files") or []:
        path = _nfc(entry.get("path"))
        if entry.get("op") == "put":
            files.append([path, "put", _sha(str(entry.get("content") or "").encode("utf-8"))])
        else:
            files.append([path, entry.get("op"), _nfc(entry.get("superseded_by")), entry.get("reason")])
    reviews = [
        [_nfc(review.get("path")), review.get("verdict"),
         _sha(review["content"].encode("utf-8")) if isinstance(review.get("content"), str) else None,
         review.get("note")]
        for review in request.get("reviews") or []
    ]
    payload = {
        "kind": request.get("kind"),
        "intent": request.get("intent"),
        "work_items": list(request.get("work_items") or []),
        "close_items": request.get("close_items", True),
        "evidence": None if evidence is None else {
            "id": evidence.get("id"),
            "item_files": [[name, (evidence_sha256 or {}).get(name)] for name in evidence.get("item_files") or []],
            "upload": _sha(_upload_bytes(evidence["upload"])) if evidence.get("upload") else None,
        },
        "files": sorted(files, key=lambda item: str(item[0])),
        "reviews": sorted(reviews, key=lambda item: str(item[0])),
    }
    return _sha(_canonical(payload).encode("utf-8"))


def error_key(error: str) -> tuple[str, str]:
    """``(rel, message with digit runs as #)`` for a ``"<rel>: <message>"`` validator string."""
    head, separator, tail = error.partition(": ")
    rel, message = (head, tail) if separator and " " not in head else ("", error)
    return rel, _DIGITS.sub("#", message)


def error_code(message: str) -> str:
    """Map a validator, policy or lint message to its changeset error code."""
    return next((code for pattern, code in _MESSAGE_CODES if pattern.search(message)), "validation")


def _from_message(error: str) -> dict:
    rel, _key = error_key(error)
    message = error[len(rel) + 2:] if rel else error
    return _error(error_code(message), message, rel or None)


class _Locating(yaml.SafeLoader):
    """A safe loader that remembers the node it constructs, to locate a value it cannot build."""

    node: yaml.Node | None = None

    def construct_object(self, node, deep=False):
        self.node = node
        return super().construct_object(node, deep=deep)


def _hint(message: str, default: str) -> str:
    return next((hint for text, hint in _YAML_HINTS if text in message), default)


def _at(mark: yaml.Mark | None) -> dict:
    """File line and column of a frontmatter mark (the frontmatter starts on line 2)."""
    return {} if mark is None else {"line": mark.line + 2, "column": mark.column + 1}


def _load_error(path: str, text: str, *, syntax: bool = True) -> dict | None:
    """A located ``yaml_parse`` error when the frontmatter does not load, else None.

    With ``syntax=False`` only values YAML parses but cannot construct are reported.
    """
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if not lines or lines[0].strip() != "---" or end is None:
        return None
    loader = _Locating("\n".join(lines[1:end]))
    try:
        loader.get_single_data()
    except yaml.MarkedYAMLError as marked:
        if not syntax:
            return None
        problem = marked.problem or str(marked)
        mark = marked.problem_mark or marked.context_mark
        return _error("yaml_parse", problem, path, **_at(mark), hint=_hint(problem, "fix the YAML at this line"))
    except yaml.YAMLError:
        return None
    except _CONSTRUCTOR_ERRORS as invalid:
        detail = str(invalid) if isinstance(invalid, ValueError) else "its tag does not fit the value"
        return _error("yaml_parse", f"YAML cannot read this value: {detail}", path,
                      **_at(loader.node.start_mark if loader.node else None),
                      hint="write a real date or value, or quote it as a string")
    finally:
        loader.dispose()
    return None


def _yaml_error(path: str, text: str, exc: Exception) -> dict:
    """A ``yaml_parse`` error, located in the file (1-based line and column) when possible."""
    line = getattr(exc, "line", None)
    return _load_error(path, text) or _error(
        "yaml_parse", str(exc), path, **({"line": line} if line else {}),
        hint=_hint(str(exc), "keep one --- frontmatter block at the top of the file"),
    )


def _path_error(path: str) -> dict | None:
    parts = path.split("/")
    if path.startswith("/") or "\\" in path or _CONTROL.search(path) or any(part in ("", ".", "..") for part in parts):
        return _error("path_forbidden", "path must be relative, without '..', empty segments or control characters",
                      path)
    if len(path.encode("utf-8")) > _MAX_PATH_BYTES or any(
        len(part.encode("utf-8")) > _MAX_SEGMENT_BYTES for part in parts
    ):
        return _error("path_forbidden", f"path segments are at most {_MAX_SEGMENT_BYTES} bytes and a path at most "
                      f"{_MAX_PATH_BYTES}", path)
    if parts[0] == "sources" or ".okf" in parts or parts[-1] in _SERVICE_OWNED_NAMES:
        return _error("service_owned_path", "the service writes this path", path)
    if any(part.startswith(".") for part in parts) or not path.endswith(".md"):
        return _error("path_forbidden", "only concept .md files may change", path)
    return None


def _nfc(value: object) -> object:
    return unicodedata.normalize("NFC", value) if isinstance(value, str) else value


def _allow_errors(allow: object) -> list[dict]:
    if allow is None:
        return []
    if not isinstance(allow, Mapping) or set(allow) - {"shrink", "retype"}:
        return [_error("input", "allow may only carry shrink and retype lists")]
    errors = []
    for name, entries in allow.items():
        if not isinstance(entries, list) or not all(
            isinstance(entry, Mapping) and set(entry) == {"path", "reason"}
            and isinstance(entry["path"], str) and isinstance(entry["reason"], str) and entry["reason"].strip()
            for entry in entries
        ):
            errors.append(_error("input", f"allow.{name} must list {{path, reason}} with a non-empty reason"))
    return errors


def _evidence_errors(evidence: object, work_items: list) -> list[dict]:
    if not isinstance(evidence, Mapping) or set(evidence) - {"id", "title", "item_files", "upload"}:
        return [_error("input", "evidence must be {id, title?, item_files | upload}")]
    errors = []
    if not isinstance(evidence.get("id"), str) or not _EVIDENCE_ID.fullmatch(evidence["id"]):
        errors.append(_error("input", "evidence.id must be 1-80 letters, digits, '-' or '_'"))
    if "title" in evidence and not isinstance(evidence["title"], str):
        errors.append(_error("input", "evidence.title must be a string"))
    item_files, upload = evidence.get("item_files"), evidence.get("upload")
    if (item_files is None) == (upload is None):
        errors.append(_error("input", "evidence needs exactly one of item_files or upload"))
    elif item_files is not None:
        matches = [_ITEM_FILE.fullmatch(name) if isinstance(name, str) else None for name in item_files] \
            if isinstance(item_files, list) else []
        if not matches or not all(matches) or len(set(item_files)) != len(item_files):
            errors.append(_error("input", "evidence.item_files must list distinct <item id>/<file> names"))
        elif any(match.group(1) not in work_items for match in matches):
            errors.append(_error("input", "every evidence.item_files item must be listed in work_items"))
    else:
        if not isinstance(upload, Mapping) or set(upload) != {"filename", "content_b64"} or not all(
            isinstance(upload[key], str) for key in upload
        ) or not re.fullmatch(_FILE_NAME, upload["filename"]):
            errors.append(_error("input", "evidence.upload must be {filename, content_b64} with a plain file name"))
        else:
            try:
                _upload_bytes(upload)
            except (binascii.Error, ValueError):
                errors.append(_error("input", "evidence.upload.content_b64 is not valid base64"))
    return errors


def _file_errors(files: object, lim: dict[str, int]) -> list[dict]:
    if not isinstance(files, list) or not files:
        return [_error("input", "files must be a non-empty list")]
    errors: list[dict] = []
    paths: list[str] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            errors.append(_error("input", f"files[{index}] must be a mapping with a path"))
            continue
        path, op = _nfc(entry["path"]), entry.get("op")
        paths.append(path)
        if isinstance(op, str) and op in _REMOVALS:
            errors.append(_error("delete_forbidden", f"op {op!r} is not allowed; concepts are never "
                                 "deleted or renamed", path))
            continue
        if op == "put":
            if set(entry) - {"path", "op", "base", "content"} or not isinstance(entry.get("content"), str):
                errors.append(_error("input", "put takes path, base and a string content", path))
            elif len(entry["content"].encode("utf-8")) > lim["concept_bytes"]:
                errors.append(_error("too_large", f"content exceeds {lim['concept_bytes']} bytes", path))
            base = entry.get("base")
        elif op == "deprecate":
            if set(entry) != {"path", "op", "base", "superseded_by", "reason"} or not all(
                isinstance(entry[key], str) and entry[key].strip() for key in ("superseded_by", "reason")
            ):
                errors.append(_error("input", "deprecate takes path, base, superseded_by and reason", path))
            elif _path_error(_nfc(entry["superseded_by"])):
                # Checked before anything reads it: the successor must be a concept path.
                errors.append(_error("broken_link", f"superseded_by {entry['superseded_by']!r} is not a concept path",
                                     path))
            base = entry.get("base")
            if base is None:
                errors.append(_error("input", "deprecate needs the base it deprecates", path))
        else:
            errors.append(_error("input", "op must be put or deprecate", path))
            continue
        if base is not None and (not isinstance(base, str) or not _CONTENT_HASH.fullmatch(base)):
            errors.append(_error("input", "base must be null or ch1:<sha256>", path))
        path_error = _path_error(path)
        if path_error:
            errors.append(path_error)
    if len(set(paths)) != len(paths):
        errors.append(_error("input", "each path may appear once per changeset"))
    if len(files) > lim["files"]:
        errors.append(_error("too_large", f"a changeset carries at most {lim['files']} files"))
    if sum(1 for entry in files if isinstance(entry, Mapping) and entry.get("op") == "deprecate") > lim["deprecates"]:
        errors.append(_error("too_large", f"a changeset deprecates at most {lim['deprecates']} concepts"))
    return errors


def _review_errors(reviews: object, lim: dict[str, int]) -> list[dict]:
    if not isinstance(reviews, list) or not reviews:
        return [_error("input", "reviews must be a non-empty list")]
    errors = []
    if len(reviews) > lim["reviews"]:
        errors.append(_error("too_large", f"an audit changeset carries at most {lim['reviews']} reviews"))
    for index, review in enumerate(reviews):
        if not isinstance(review, Mapping) or not isinstance(review.get("path"), str):
            errors.append(_error("input", f"reviews[{index}] must be a mapping with a path"))
            continue
        path = _nfc(review["path"])
        corrected = review.get("verdict") == "corrected"
        allowed = {"path", "base", "verdict", "note"} | ({"content"} if corrected else set())
        if (
            set(review) - allowed or review.get("verdict") not in VERDICTS
            or not isinstance(review.get("base"), str) or not _CONTENT_HASH.fullmatch(review["base"])
            or not isinstance(review.get("note", ""), str) or corrected != isinstance(review.get("content"), str)
        ):
            errors.append(_error("input", "a review is {path, base, verdict, note}; corrected adds content", path))
        errors.extend(filter(None, [_path_error(path)]))
    return errors


def check_request(request: object, lim: dict[str, int] | None = None) -> list[dict]:
    """G1: schema, limits and path syntax. Returns errors; an empty list admits the request."""
    lim = lim or limits()
    if not isinstance(request, Mapping):
        return [_error("input", "a changeset must be a JSON object")]
    try:
        _canonical(request).encode("utf-8")
    except (TypeError, ValueError):  # a lone surrogate (JSON "\ud800") or a non-JSON key
        return [_error("input", "a changeset must be JSON whose strings are valid UTF-8")]
    errors = []
    kind = request.get("kind")
    if request.get("schema") != SCHEMA:
        errors.append(_error("input", f"schema must be {SCHEMA!r}"))
    if not isinstance(kind, str) or kind not in _KEYS:
        return errors + [_error("input", "kind must be curate or audit")]
    unknown = sorted(set(request) - _KEYS[kind])
    if unknown:
        errors.append(_error("input", f"unknown {kind} changeset field(s): {', '.join(map(str, unknown))}"))
    if not isinstance(request.get("base_revision"), str) or not _REVISION.fullmatch(request["base_revision"]):
        errors.append(_error("input", "base_revision must be a commit sha"))
    for key in ("run", "message"):
        if key in request and not isinstance(request[key], str):
            errors.append(_error("input", f"{key} must be a string"))
    if kind == "audit":
        return errors + _review_errors(request.get("reviews"), lim)
    if request.get("intent") == "restructure":
        state = "is not implemented by this gate" if os.environ.get("AIWIKI_RESTRUCTURE") == "on" \
            else "is disabled (AIWIKI_RESTRUCTURE=off)"
        errors.append(_error("input", f"intent restructure {state}"))
    elif request.get("intent") != "evidence":
        errors.append(_error("input", "intent must be evidence"))
    work_items = request.get("work_items", [])
    if not isinstance(work_items, list) or not all(isinstance(item, str) and _WORK_ITEM.fullmatch(item)
                                                   for item in work_items) or len(set(work_items)) != len(work_items):
        errors.append(_error("input", "work_items must list distinct it_<id> work items"))
        work_items = []
    if not isinstance(request.get("close_items", True), bool):
        errors.append(_error("input", "close_items must be a boolean"))
    if request.get("intent") == "evidence":
        errors.extend(_evidence_errors(request.get("evidence"), work_items))
    errors.extend(_allow_errors(request.get("allow")))
    errors.extend(_file_errors(request.get("files"), lim))
    return errors


def _label(part: Mapping) -> str:
    kind = part.get("kind")
    if kind == "git-file" and part.get("remote") and part.get("commit") and part.get("path"):
        repository = str(part["remote"]).rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        return f"{repository}@{str(part['commit'])[:7]}:{part['path']}"
    if kind == "multica-comment" and part.get("issue"):
        return f"{part['issue']}#{part['comment']}" if part.get("comment") else str(part["issue"])
    return f"{part['item']}/{part['file']}" if part.get("item") else str(part.get("file"))


def build_packet(
    evidence: Mapping,
    evidence_files: Sequence[EvidenceFile] = (),
    lim: dict[str, int] | None = None,
) -> tuple[Packet | None, list[dict]]:
    """Assemble the changeset's one evidence packet from frozen bytes (§2.2 rule 1, §5.6).

    One file keeps its bytes and extension; several text files become one Markdown
    source whose front matter lists each part's origin. Only frozen bytes enter a
    packet, never agent-written text.
    """
    lim = lim or limits()
    evidence_id = str(evidence["id"])
    if evidence.get("upload"):
        upload = evidence["upload"]
        data = _upload_bytes(upload)
        members = [(upload["filename"], data, {"ref": "S1", "kind": "upload", "file": upload["filename"]})]
    else:
        provided = {item.name: item for item in evidence_files}
        missing = [name for name in evidence["item_files"] if name not in provided]
        if missing:
            return None, [_error("input", f"evidence file(s) not provided: {', '.join(missing)}")]
        members = []
        for index, name in enumerate(evidence["item_files"], start=1):
            item, file = name.split("/", 1)
            data = provided[name].data
            ours = {"ref": f"S{index}", "item": item, "file": file, "sha256": _sha(data)}
            members.append((file, data, {**ours, **{
                key: value for key, value in provided[name].origin.items() if key not in ours
            }}))
    for _name, data, part in members:
        part.setdefault("sha256", _sha(data))
        if not data.strip():
            return None, [_error("input", f"evidence part {part['ref']} is empty")]
    texts = []
    for _name, data, _part in members:
        try:
            texts.append(data.decode("utf-8"))
        except UnicodeDecodeError:
            texts.append(None)
    if len(members) == 1:
        name, data, part = members[0]
        filename, parts = f"{evidence_id}{Path(name).suffix}", (part,)
    elif any(text is None for text in texts):
        return None, [_error("input", "a packet of several parts takes UTF-8 text files only")]
    else:
        header = {"ai_wiki_evidence": 1, "id": evidence_id}
        if evidence.get("title"):
            header["title"] = evidence["title"]
        header["parts"] = [part for _name, _data, part in members]
        sections = "".join(
            f"## {part['ref']} · {_label(part)}\n\n{text}{'' if text.endswith(chr(10)) else chr(10)}\n"
            for (_name, _data, part), text in zip(members, texts, strict=True)
        )
        dumped = yaml.safe_dump(header, sort_keys=False, allow_unicode=True, width=4096)
        data = ("---\n" + dumped + "---\n" + sections).rstrip("\n").encode("utf-8") + b"\n"
        filename, parts = f"{evidence_id}.md", tuple(part for _name, _data, part in members)
    text_packet = len(members) > 1 or texts[0] is not None
    limit = lim["packet_text_bytes"] if text_packet else lim["packet_binary_bytes"]
    if len(data) > limit:
        return None, [_error("too_large", f"the evidence packet exceeds {limit} bytes")]
    return Packet(filename=filename, data=data, parts=parts), []


def _concept_names(bundle: Path) -> dict[str, str]:
    """Normalized title/alias -> path for every live (non-deprecated) concept."""
    names: dict[str, str] = {}
    for rel, frontmatter in _frontmatters(bundle).items():
        if frontmatter.get("status") != "deprecated":
            for name in _names(frontmatter):
                names.setdefault(name, rel)
    return names


def _frontmatters(bundle: Path) -> dict[str, dict]:
    parsed = {}
    for path in sorted(bundle.rglob("*.md")):
        rel = path.relative_to(bundle).as_posix()
        if path.is_symlink() or not should_check(path, bundle):
            continue
        try:
            parsed[rel] = parse_doc(path)[0]
        except (OSError, UnicodeError, ValueError):
            continue
    return parsed


def _names(frontmatter: Mapping) -> set[str]:
    aliases = frontmatter.get("aliases") if isinstance(frontmatter.get("aliases"), list) else []
    return {
        re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", value).casefold())
        for value in [frontmatter.get("title"), *aliases] if isinstance(value, str) and value.strip()
    }


def _frontmatter(text: str) -> dict | None:
    try:
        return parse_document(text).frontmatter
    except (OKFDocumentError, *_CONSTRUCTOR_ERRORS):
        return None


def _service_blocks(text: str | None) -> dict[str, object]:
    """The service keys ``text`` writes, each as its own block reads (a broken block reads invalid)."""
    if text is None:
        return {}
    blocks = bookkeeping._blocks(bookkeeping._split(text)[0])
    return {key: bookkeeping._value(lines) for key in SERVICE_KEYS
            if (lines := bookkeeping._lines(blocks, key)) is not None}


def _body(text: str) -> str:
    try:
        return parse_document(text).body.strip()
    except OKFDocumentError:
        return ""


def _base_checks(bundle: Path, files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Filesystem path rules and the per-file CAS against the base (G6)."""
    existing: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(bundle):
        dirnames[:] = [name for name in dirnames if name not in (".git", ".okf")]
        for name in dirnames + filenames:
            rel = unicodedata.normalize("NFC", (Path(directory) / name).relative_to(bundle).as_posix())
            existing.setdefault(rel.casefold(), rel)
    errors, conflicts = [], []
    seen: dict[str, str] = {}
    paths = {entry["path"] for entry in files}
    for entry in files:
        rel, target = entry["path"], bundle / entry["path"]
        parts = rel.split("/")
        prefixes = ["/".join(parts[:end]) for end in range(1, len(parts) + 1)]
        twins = [
            other for prefix in prefixes
            if (other := existing.get(prefix.casefold(), seen.get(prefix.casefold(), prefix))) != prefix
        ]
        seen.update((prefix.casefold(), prefix) for prefix in prefixes)
        if twins:
            errors.append(_error("path_forbidden", f"path differs only in case from {twins[0]}", rel))
            continue
        if any(prefix in paths or ((bundle / prefix).exists() and not (bundle / prefix).is_dir())
               for prefix in prefixes[:-1]):
            errors.append(_error("path_forbidden", "a parent of this path is a file", rel))
            continue
        if has_symlink_component(bundle, target) or (target.exists() and not target.is_file()):
            errors.append(_error("path_forbidden", "path must be a regular file, not a symlink or directory", rel))
            continue
        text = target.read_bytes().decode("utf-8", errors="replace") if target.is_file() else None
        current = content_hash(text) if text is not None else None
        status = (_frontmatter(text) or {}).get("status") if text is not None else None
        message = None
        if entry["base"] is None and current is not None:
            message = "the concept already exists; send its base to update it"
        elif entry["base"] is not None and current is None:
            message = "the concept does not exist at the base"
        elif entry["base"] != current:
            message = "the concept changed since its base"
        elif status == "deprecated":
            message = "the concept is deprecated"
        if message:
            conflicts.append({"path": rel, "base": entry["base"], "current": current})
            errors.append(_error("conflict", message, rel, base=entry["base"], current=current))
    return errors, conflicts


def _deprecation(before: str, entry: dict, title: str, now: datetime) -> str:
    """HEAD bytes with a dated deprecation note on top of the body (§2.2 rule 3)."""
    lines = before.splitlines(keepends=True)
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    head, body = "".join(lines[: end + 1]), "".join(lines[end + 1:])
    link = posixpath.relpath(entry["superseded_by"], posixpath.dirname(entry["path"]) or ".")
    reason = " ".join(entry["reason"].split())
    note = f"> Deprecated {now:%Y-%m-%d}: superseded by [{title}]({link}). {reason}\n\n"
    return head + ("" if head.endswith("\n") else "\n") + note + body.lstrip("\n")


def _source_key(source: Mapping) -> str:
    return str(source.get("resource") or "").strip()


def _failure(status: int, errors: list[dict]) -> dict:
    detail = "; ".join(" ".join(filter(None, (error["code"], error.get("path")))) for error in errors[:5])
    if status == 409:
        return failure("conflict", stage="validation", detail=detail)
    if status in (400, 413):
        return failure("input", stage="intake", detail=detail)
    if any(error["code"] == "secret_detected" for error in errors):
        # The same bytes fail every retry: the item is parked, not counted (§2.8).
        return failure("input", stage="validation", detail=detail)
    return failure("model_output", stage="validation", detail=detail)


def _redacted(value: object) -> object:
    """``value`` with every secret-rule match replaced, in keys and strings alike."""
    if isinstance(value, str):
        return secrets.redact(value)[0]
    if isinstance(value, Mapping):
        return {_redacted(key): _redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def _rejected(result: dict, errors: list[dict]) -> dict:
    result.pop("files", None)  # a rejection never echoes content
    errors = sorted(errors, key=lambda error: (error.get("path", ""), error.get("line", 0), error["code"]))
    status = min((CODES[error["code"]] for error in errors), key=lambda code: (code != 400, code != 413, code))
    result.update(status="rejected", http_status=status, errors=errors, failure=_failure(status, errors))
    # Messages, repairs and paths quote the agent's text: never echo a secret they carry.
    return _redacted(result)


def evaluate(
    base_dir: Path,
    request: Mapping,
    *,
    actor: str,
    now: datetime,
    evidence_files: Sequence[EvidenceFile] = (),
) -> dict:
    """Judge one curate changeset against ``base_dir`` without changing it (G1, G6, G8–G12).

    ``actor`` is the token's actor stamped as ``generated.by``; ``now`` is the trusted
    service time. ``evidence_files`` carries the frozen bytes of ``evidence.item_files``.
    The result's ``status`` is ``would_apply``, ``noop`` or ``rejected``; ``files`` holds
    the stamped bytes of every file the changeset writes and ``source`` the packet path.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    lim = limits()
    result: dict = {"status": "rejected", "errors": [], "warnings": [], "validation": {"status": "not_run"}}
    errors = check_request(request, lim)
    if not errors and request["kind"] == "audit":
        errors = [_error("input", "audit changesets are judged by the audit gate, not by evaluate()")]
    packet = None
    if not errors:
        packet, errors = build_packet(request["evidence"], evidence_files, lim)
    if errors:
        return _rejected(result, errors)
    shas = {item.name: _sha(item.data) for item in evidence_files}
    evidence_id = request["evidence"]["id"]
    result.update(
        changeset_sha256=changeset_sha256(request, shas),
        kind="curate", intent=request["intent"], work_items=list(request.get("work_items") or []),
        evidence={"id": evidence_id, "parts": len(packet.parts),
                  "origin_kinds": sorted({str(part.get("kind") or "item-file") for part in packet.parts})},
        normalizations=[],
    )
    files = []
    for entry in request["files"]:
        path = _nfc(entry["path"])
        if path != entry["path"]:
            result["normalizations"].append({"path": path, "key": "path", "from": entry["path"], "to": path,
                                             "action": "nfc_normalized"})
        files.append({**entry, "path": path, "base": entry.get("base")})
    files.sort(key=lambda entry: entry["path"])
    base = Path(base_dir)
    errors, conflicts = _base_checks(base, files)
    if errors:
        if conflicts:
            result["conflicts"] = conflicts
        return _rejected(result, errors)
    temporary, workspace = policy._isolated_agent_bundle(base)
    try:
        return _evaluate_in(workspace, files, request, packet, result, actor=actor, now=now, lim=lim)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _evaluate_in(
    workspace: Path, files: list[dict], request: Mapping, packet: Packet, result: dict, *, actor: str, now: datetime,
    lim: dict[str, int],
) -> dict:
    evidence_id = request["evidence"]["id"]
    allow = request.get("allow") or {}
    allowed = {name: {_nfc(entry["path"]) for entry in allow.get(name, [])} for name in ("shrink", "retype")}
    before_errors = validate(workspace)
    before_concepts = policy._concept_snapshot(workspace)
    sources_before = policy._source_snapshot(workspace)
    tree_before = policy._agent_tree_snapshot(workspace)
    links_before = policy._agent_symlink_snapshot(workspace)
    names = _concept_names(workspace)
    deprecating = {entry["path"] for entry in files if entry["op"] == "deprecate"}

    # G7: the packet lands where closeout will move it, under its content-addressed name.
    inbox_rel, packet_sha = write_source(workspace, packet.data, packet.filename)
    packet_rel = "sources/" + Path(inbox_rel).name
    os.replace(workspace / inbox_rel, workspace / packet_rel)
    result.update(source=packet_rel, sha256=packet_sha)
    try:
        packet_text = packet.data.decode("utf-8")
    except UnicodeDecodeError:
        packet_text = ""
    errors: list[dict] = [
        _error("secret_detected", f"evidence matches secret rule {rule}", packet_rel, line=line, rule=rule)
        for rule, line in secrets.scan(packet_text)
    ]
    # message and run become the commit message: permanent, mirrored history.
    errors += [_error("secret_detected", f"{key} matches secret rule {rule}", line=line, rule=rule)
               for key in ("message", "run") for rule, line in secrets.scan(request.get(key) or "")]

    puts = {entry["path"]: entry["content"] for entry in files if entry["op"] == "put"}
    packet_resource = "/" + packet_rel
    written: dict[str, str] = {}
    unparsed: set[str] = set()
    repairs: dict[str, list[str]] = {}
    preview: dict[str, list[str]] = {}
    noop: list[str] = []
    for entry in files:
        rel, target = entry["path"], workspace / entry["path"]
        before = target.read_text(encoding="utf-8") if target.is_file() else None
        head = (_frontmatter(before) or {}) if before is not None else {}
        head_sources = [source for source in head["sources"] if isinstance(source, Mapping)] \
            if isinstance(head.get("sources"), list) else []
        deprecate = entry["op"] == "deprecate"
        if deprecate and not head:
            errors.append(_yaml_error(rel, before, OKFDocumentError("the concept to deprecate does not parse")))
            continue
        if any(source.get("id") == evidence_id and _source_key(source) != packet_resource for source in head_sources):
            errors.append(_error("invalid_value", f"evidence.id {evidence_id!r} already names another source of "
                                 "this concept", rel, hint="choose an evidence.id this concept's sources do not use"))
            continue
        if deprecate:
            # G8: the service writes a deprecation from HEAD; it cites the packet itself.
            successor = entry["superseded_by"] = _nfc(entry["superseded_by"])
            successor_path = workspace / successor  # G1 made it a relative concept path
            successor_text = puts.get(successor) or (
                successor_path.read_bytes().decode("utf-8", errors="replace")
                if successor_path.is_file() and not has_symlink_component(workspace, successor_path) else None
            )
            successor_fm = _frontmatter(successor_text) if successor_text else None
            if (
                successor == rel or successor_fm is None or successor_fm.get("status") == "deprecated"
                or successor in deprecating
            ):
                errors.append(_error("broken_link", f"superseded_by {successor!r} is not a live concept", rel))
                continue
            edited = _deprecation(before, entry, str(successor_fm.get("title") or successor), now)
            if packet_resource not in {_source_key(source) for source in head_sources}:
                edited = bookkeeping.append_sources(edited, [{"id": evidence_id, "resource": PACKET_RESOURCE}])
        else:
            edited = entry["content"]
            unreadable = _load_error(rel, edited, syntax=False)
            if unreadable:  # no later reader expects a value YAML parses but cannot construct
                errors.append(unreadable)
                continue
        # A deprecation is scanned too: its reason is agent-written.
        errors.extend(_error("secret_detected", f"content matches secret rule {rule}", rel, line=line, rule=rule)
                      for rule, line in secrets.scan(edited))
        try:
            # G9. The first pass puts HEAD's service-owned blocks back, so the packet rewrite and the
            # source restore read the agent's content keys, whatever it wrote into those blocks.
            text, stamped = bookkeeping.apply_bookkeeping(
                before, edited, actor=actor, trusted_now=now, stage="changeset", deprecate=deprecate,
            )
            sources = parse_document(text).frontmatter.get("sources")
            sources = sources if isinstance(sources, list) else []
            for index, source in enumerate(sources):
                resource = _source_key(source) if isinstance(source, Mapping) else ""
                cited = resource == PACKET_RESOURCE and source.get("id") == evidence_id
                if resource.startswith("evidence:") and not cited:
                    errors.append(_error("unknown_evidence_ref", f"sources[{index}] cites {resource!r} as "
                                         f"{source.get('id')!r}; this changeset's packet is {evidence_id!r}", rel))
            text, indices = bookkeeping.rewrite_source_resource(
                text, placeholder=PACKET_RESOURCE, resource=packet_resource,
            )
            result["normalizations"].extend(
                {"path": rel, "key": f"sources[{index}].resource", "from": PACKET_RESOURCE, "to": packet_resource}
                for index in indices
            )
            kept = {_source_key(source) for source in sources if isinstance(source, Mapping)}
            dropped = [source for source in head_sources if _source_key(source) not in kept]
            if dropped:
                text = bookkeeping.append_sources(text, dropped)
                result["normalizations"].append({"path": rel, "key": "sources", "action": "restored_from_head",
                                                 "resources": [_source_key(source) for source in dropped]})
            final, _again = bookkeeping.apply_bookkeeping(
                before, text, actor=actor, trusted_now=now, stage="changeset", deprecate=deprecate,
            )
        except (bookkeeping.BookkeepingError, OKFDocumentError) as exc:
            errors.append(_yaml_error(rel, edited, exc))
            unparsed.add(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edited, encoding="utf-8")  # later gates still see the file
            continue
        if final == before:
            noop.append(rel)
            continue
        if len(final.encode("utf-8")) > lim["concept_bytes"]:
            errors.append(_error("too_large", f"the stamped concept exceeds {lim['concept_bytes']} bytes", rel))
            continue
        if stamped:
            repairs[rel] = stamped
        after = _frontmatter(final) or {}
        preview[rel] = ["generated stamped"] + (
            [f"status={after.get('status')}"] if before is None or deprecate else []
        )
        if not deprecate:
            mine, theirs, ours = (_service_blocks(version) for version in (edited, before, final))
            result["warnings"].extend(
                {"code": "service_owned_key_ignored", "path": rel, "key": key}
                for key in sorted(mine) if mine[key] != theirs.get(key) and mine[key] != ours.get(key)
            )
        if before is not None and not deprecate:
            locked = [key for key in ("type", "title") if after.get(key) != head.get(key)]
            if locked and rel not in allowed["retype"]:
                errors.append(_error("identity_locked", f"{' and '.join(locked)} changed", rel))
            head_body, body = _body(before), _body(final)
            if head_body and len(body) < SHRINK_RATIO * len(head_body) and rel not in allowed["shrink"]:
                errors.append(_error("body_shrink", f"body shrank to {len(body)} of {len(head_body)} characters",
                                     rel))
        if before is None:
            clashes = sorted(name for name in _names(after) if names.get(name, rel) not in (rel, *deprecating))
            if clashes:
                errors.append(_error("duplicate_title", f"title or alias matches {names[clashes[0]]}", rel))
            for name in _names(after):
                names.setdefault(name, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(final, encoding="utf-8")
        written[rel] = final

    changed = sorted(set(written) | unparsed)
    deprecated_files = [entry["path"] for entry in files if entry["op"] == "deprecate" and entry["path"] in written]
    result.update(
        files=written,
        content_hashes={rel: content_hash(text) for rel, text in written.items()},
        concept_files=[rel for rel in sorted(written) if rel not in deprecated_files],
        deprecated_files=deprecated_files, noop_files=noop,
        deterministic_repairs=repairs, bookkeeping_preview=preview,
    )

    # G10 scope, G11 policy and G12 validation judge the whole stamped workspace.
    judged = (
        policy._agent_scope_errors(workspace, tree_before, links_before, packet_rel, packet_sha)
        + policy._source_policy_errors(workspace, sources_before, packet_sha)
        + policy._curation_policy_errors(workspace, before_concepts, None, actor=actor)
        + policy._curation_provenance_errors(workspace, before_concepts, packet_rel)
    )
    before_keys = {error_key(error) for error in before_errors}
    baseline = []
    new_errors = 0
    for error in validate(workspace):
        rel, _message = error_key(error)
        if rel in unparsed:
            continue  # its yaml_parse error already says why
        if rel in changed or error_key(error) not in before_keys:
            judged.append(error)
            new_errors += 1
        else:
            baseline.append(error)
    judged += validate_changed(workspace, sorted(written))
    findings, _count = lint(workspace)
    judged += [
        f"{finding['where']}: {finding['detail']}" for finding in findings
        if finding["severity"] == "high" and finding["where"] in changed
    ]
    errors += [error for error in map(_from_message, dict.fromkeys(judged)) if error.get("path") not in unparsed]
    result["warnings"] += [
        {"code": "baseline_error_untouched", **{key: error[key] for key in ("path", "message") if key in error}}
        for error in map(_from_message, baseline)
    ]
    result["validation"] = {
        "status": "failed" if errors else "passed", "new_errors": new_errors, "baseline_errors": len(baseline),
    }
    if errors:
        return _rejected(result, list({_canonical(error): error for error in errors}.values()))
    result.update(status="would_apply" if written else "noop", http_status=200)
    return result
