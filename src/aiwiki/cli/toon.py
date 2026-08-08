"""Small, stdlib-only TOON output helpers for the CLI boundary.

The CLI only needs objects and scalar tables, so a full general-purpose codec would be
unnecessary weight. Strings are always quoted, which is valid TOON and avoids delimiter
and type ambiguity in agent-facing output.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

_BARE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def key(value: str) -> str:
    return value if _BARE_KEY.fullmatch(value) else scalar(value)


def object_lines(name: str | None, values: Mapping[str, Any], *, indent: int = 0) -> list[str]:
    pad = " " * indent
    lines = [f"{pad}{key(name)}:"] if name else []
    child = indent + 2 if name else indent
    cpad = " " * child
    for field, value in values.items():
        if isinstance(value, Mapping):
            lines.extend(object_lines(field, value, indent=child))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if all(_is_scalar(item) for item in value):
                encoded = ",".join(scalar(item) for item in value)
                lines.append(f"{cpad}{key(field)}[{len(value)}]:" + (f" {encoded}" if encoded else ""))
            else:
                # This path is rare for current API responses. Preserve the value without
                # pretending a heterogeneous shape is a scalar table.
                lines.append(f"{cpad}{key(field)}: {scalar(json.dumps(value, ensure_ascii=False))}")
        else:
            lines.append(f"{cpad}{key(field)}: {scalar(value)}")
    return lines


def table_lines(name: str, rows: Iterable[Mapping[str, Any]], fields: Sequence[str], *, indent: int = 0) -> list[str]:
    data = list(rows)
    pad = " " * indent
    header = ",".join(key(field) for field in fields)
    lines = [f"{pad}{key(name)}[{len(data)}]{{{header}}}:"]
    for row in data:
        lines.append(f"{pad}  " + ",".join(scalar(row.get(field)) for field in fields))
    return lines


def emit(*groups: Iterable[str]) -> None:
    lines: list[str] = []
    for group in groups:
        block = list(group)
        if not block:
            continue
        if lines:
            lines.append("")
        lines.extend(block)
    print("\n".join(lines))


def emit_error(message: str, *, kind: str = "error", usage: str | None = None,
               commands: Sequence[str] = ()) -> None:
    values = {"kind": kind, "message": message}
    if usage:
        values["usage"] = " ".join(usage.split())
    groups: list[list[str]] = [object_lines("error", values)]
    if commands:
        groups.append(table_lines("help", ({"command": command} for command in commands), ("command",)))
    emit(*groups)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))
