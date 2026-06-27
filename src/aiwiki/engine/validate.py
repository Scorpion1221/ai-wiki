#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REQUIRED = [
    'type', 'title', 'description', 'tags', 'timestamp',
    'status', 'confidence', 'source_type', 'source_ref'
]
RESERVED = {'index.md', 'log.md', 'SCHEMA.md', 'purpose.md'}
LINK_RE = re.compile(r'\]\(([^)\s]+\.md)(?:#[^)]+)?\)')
VALID_STATUS = {'draft', 'reviewed', 'canonical', 'stale'}
VALID_CONFIDENCE = {'low', 'medium', 'high'}


def parse_doc(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding='utf-8')
    if not text.startswith('---\n'):
        raise ValueError('missing YAML frontmatter')
    end = text.find('\n---\n', 4)
    if end < 0:
        raise ValueError('unterminated YAML frontmatter')
    fm_text = text[4:end]
    body = text[end + 5:]
    fm = yaml.safe_load(fm_text) or {}
    if not isinstance(fm, dict):
        raise ValueError('frontmatter must be a mapping')
    return fm, body


def should_check(path: Path, root: Path) -> bool:
    if path.name in RESERVED or path.name.startswith('log-'):
        return False
    parts = path.relative_to(root).parts
    if 'sources' in parts or '.okf' in parts:
        return False
    return path.suffix == '.md'


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    if not (root / 'index.md').exists():
        errors.append('missing root index.md')
    docs = [p for p in sorted(root.rglob('*.md')) if should_check(p, root)]
    for path in docs:
        rel = path.relative_to(root)
        try:
            fm, body = parse_doc(path)
        except Exception as e:
            errors.append(f'{rel}: {e}')
            continue
        for key in REQUIRED:
            if fm.get(key) in (None, '', []):
                errors.append(f'{rel}: missing required frontmatter key {key}')
        if fm.get('status') and fm.get('status') not in VALID_STATUS:
            errors.append(f'{rel}: invalid status {fm.get("status")!r}')
        if fm.get('confidence') and fm.get('confidence') not in VALID_CONFIDENCE:
            errors.append(f'{rel}: invalid confidence {fm.get("confidence")!r}')
        if '# Citations' not in body:
            errors.append(f'{rel}: missing # Citations section')
        for link in LINK_RE.findall(body):
            if '://' in link or link.startswith('/'):
                continue
            target = (path.parent / link).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                errors.append(f'{rel}: link escapes bundle: {link}')
                continue
            if not target.exists():
                errors.append(f'{rel}: broken link: {link}')
    if not docs:
        errors.append('no concept docs found')
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Validate an OKF bundle.')
    ap.add_argument('bundle', type=Path)
    a = ap.parse_args(argv)
    root = a.bundle.expanduser().resolve()
    if not root.is_dir():
        print(f'not a directory: {root}', file=sys.stderr)
        return 2
    errors = validate(root)
    if errors:
        for e in errors:
            print(f'ERROR: {e}', file=sys.stderr)
        print(f'FAILED: {len(errors)} error(s)', file=sys.stderr)
        return 1
    print(f'OK: {root}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
