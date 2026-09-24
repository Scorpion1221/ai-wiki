"""Service-owned bookkeeping: pure text in, text out, callable without an agent.

Fixtures under ``fixtures/live_bundle`` come from the production bundle (commit
3b5731b for the orphan concept, a358395 for the four ``verified`` list shapes). Their
bookkeeping lines are byte-identical to production; business prose is redacted
because this repository is public. Set ``AIWIKI_TEST_BUNDLE_GIT`` to a bundle clone
to replay against the unredacted files.
"""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from aiwiki.engine import bookkeeping
from aiwiki.engine.document import concept_metadata, normalize_verified, parse_document
from aiwiki.engine.validate import body_spill_errors, spill_warnings, validate_profile_document
from aiwiki.engine.validate import main as validate_main

AUDITOR = "process:ai-wiki-adversarial-audit"
CURATOR = "process:ai-wiki-curator"
LIVE = Path(__file__).parent / "fixtures" / "live_bundle"
NOW = datetime(2026, 9, 24, 1, 2, 3, 456000, tzinfo=UTC)
STAMP = "2026-09-24T01:02:03Z"
ORPHAN_REL = "experiments/web-landing-page-aio-ab.md"
ORPHAN_LINE = f"  - {{by: {AUDITOR}, at: 2026-09-19T20:56:59Z}}"
# The four `verified` list shapes in the live bundle and the item the service appends.
SHAPES = {
    "metrics/web-landing-new-visitor-distribution-2026-09.md": f"- by: {AUDITOR}\n  at: '{STAMP}'\n",
    "metrics/ai-study-country-registration-payment-2026-09.md": f"  - {{by: {AUDITOR}, at: {STAMP}}}\n",
    "metrics/view-references-exposure-proxy-2026-09.md": f"  - by: {AUDITOR}\n    at: '{STAMP}'\n",
    "metrics/plugin-install-first-payment-funnel-2026-09.md": f"- {{by: {AUDITOR}, at: {STAMP}}}\n",
}


def _live(rel: str) -> str:
    return (LIVE / rel).read_text(encoding="utf-8")


def _audit(before: str, after: str, verdict: str = "verified", now: datetime = NOW) -> tuple[str, list[str]]:
    return bookkeeping.apply_bookkeeping(
        before, after, actor=AUDITOR, trusted_now=now, stage="audit", verdict=verdict,
    )


def _verified_block_end(text: str) -> int:
    lines = text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("verified:"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i][:1] not in (" ", "-"))
    return sum(len(line) for line in lines[:end])


def _append(text: str, item: str) -> str:
    end = _verified_block_end(text)
    return text[:end] + item + text[end:]


def _assert_valid_and_current(text: str) -> dict:
    document = parse_document(text)
    assert validate_profile_document(document.frontmatter, document.body) == []
    assert body_spill_errors(document.body) == []
    metadata = concept_metadata(document.frontmatter)
    assert metadata["verification_current"] is True
    return document.frontmatter


# Reviewer edits of `verified` seen in production or equally plausible. Each is the
# model hand-writing a field it does not own; none may fail the job.
VERIFIED_EDITS = {
    "mixed_indentation": lambda text: _append(text, f"   - {{by: {AUDITOR}, at: '{STAMP}'}}\n"),
    "stale_timestamp": lambda text: _append(text, f"- {{by: {AUDITOR}, at: 2026-09-19T20:56:59Z}}\n"),
    "two_events": lambda text: _append(
        text, f"- {{by: {AUDITOR}, at: {STAMP}}}\n- {{by: {AUDITOR}, at: {STAMP}}}\n",
    ),
    "history_dropped": lambda text: text[:text.index("verified:")] + "verified:\n"
    + f"  - {{by: {AUDITOR}, at: {STAMP}}}\n" + text[_verified_block_end(text):],
    "single_mapping": lambda text: text[:text.index("verified:")]
    + f"verified: {{by: {AUDITOR}, at: '{STAMP}'}}\n" + text[_verified_block_end(text):],
    "forged_human": lambda text: _append(text, "- {by: 'human:owner', at: 2026-09-24T00:00:00Z}\n"),
    # An item that lost its `- `: its fields become top-level keys of the frontmatter.
    "column0_fields": lambda text: _append(text, f"by: {AUDITOR}\nat: '{STAMP}'\n"),
    "untouched": lambda text: text,
}
EXPECTED_REPAIRS = {
    "column0_fields": [
        f"removed spilled verification field from frontmatter: {line!r}"
        for line in (f"by: {AUDITOR}", f"at: '{STAMP}'")
    ],
    "untouched": [],
}


@pytest.mark.parametrize("edit", sorted(VERIFIED_EDITS))
@pytest.mark.parametrize("rel", sorted(SHAPES))
def test_reviewer_verified_edits_in_every_live_shape_are_normalized(rel: str, edit: str) -> None:
    before = _live(rel)
    text, repairs = _audit(before, VERIFIED_EDITS[edit](before))
    # Byte-identical except one appended event in the list's own indentation/style/quoting.
    assert text == _append(before, SHAPES[rel])
    assert repairs == EXPECTED_REPAIRS.get(edit, ["restored service-owned verification history"])
    frontmatter = _assert_valid_and_current(text)
    before_events = normalize_verified(parse_document(before).frontmatter)
    assert normalize_verified(frontmatter)[:-1] == before_events


def test_single_mapping_history_becomes_a_list_only_when_appending() -> None:
    before = _live(ORPHAN_REL).replace(
        f"verified:\n  - {{by: {AUDITOR}, at: 2026-09-14T20:50:19Z}}\n"
        f"  - {{by: {AUDITOR}, at: 2026-09-17T21:18:46Z}}\n",
        f"verified:\n  by: {AUDITOR}\n  at: 2026-09-17T21:18:46Z\n",
    )
    unchanged, _repairs = _audit(before, before, verdict="unverified")
    assert "verified:\n  by: " in unchanged
    appended, _repairs = _audit(before, before)
    assert parse_document(appended).frontmatter["verified"] == [
        {"by": AUDITOR, "at": "2026-09-17T21:18:46Z"},
        {"by": AUDITOR, "at": STAMP},
    ]


def test_whitespace_order_and_quoting_edits_do_not_refresh_generation() -> None:
    before = _live("metrics/view-references-exposure-proxy-2026-09.md")
    title, description = "title: Redacted title\n", "description: Redacted description\n"
    after = (
        before.replace("last_modified: '2026-09-12'", "last_modified: 2026-09-12")  # re-quoted date
        .replace(title + description, description + title)  # key order
        .replace("# Summary\n", "# Summary   \n\n\n")  # trailing spaces and blank lines
        .replace("Redacted fixture body.\n", "Redacted fixture body.\n\n")
    )
    assert after != before
    for stage in ("audit", "curate"):
        text, repairs = bookkeeping.apply_bookkeeping(
            before, after, actor=AUDITOR if stage == "audit" else CURATOR, trusted_now=NOW,
            stage=stage, verdict="unverified" if stage == "audit" else None,
        )
        assert text == before
        assert repairs == ["discarded non-substantive formatting edits"]
        assert not bookkeeping.substantive_change(before, after, stage=stage)


def test_substantive_edit_stamps_generation_in_the_files_own_style() -> None:
    before = _live("metrics/view-references-exposure-proxy-2026-09.md")
    after = before.replace("Redacted fixture body.", "Corrected claim.").replace(
        "  by: 'process:ai-wiki-curator'\n  at: '2026-09-12T20:43:00.290519Z'",
        f"  by: {AUDITOR}\n  at: 2026-09-12T20:43:00Z",  # stale, and older than the prior generation
    )
    text, repairs = _audit(before, after)
    assert repairs == []
    assert f"generated:\n  by: '{AUDITOR}'\n  at: '{STAMP}'\n" in text
    assert text.endswith("Corrected claim.\n")
    frontmatter = _assert_valid_and_current(text)
    assert frontmatter["verified"][-1] == {"by": AUDITOR, "at": STAMP}


def test_generation_moves_past_skewed_history_so_old_verification_is_not_current() -> None:
    before = _live("metrics/ai-study-country-registration-payment-2026-09.md")
    after = before.replace("Redacted fixture body.", "Changed.")
    lagging_clock = datetime(2026, 9, 17, 21, 0, tzinfo=UTC)  # earlier than the last verification
    text, _repairs = bookkeeping.apply_bookkeeping(
        before, after, actor=CURATOR, trusted_now=lagging_clock, stage="curate",
    )
    frontmatter = parse_document(text).frontmatter
    assert frontmatter["generated"] == {"by": CURATOR, "at": "2026-09-17T21:18:47Z"}
    assert concept_metadata(frontmatter)["verification_current"] is False


def test_curation_owns_generation_and_verification_but_not_status_or_sources() -> None:
    before = _live("metrics/plugin-install-first-payment-funnel-2026-09.md")
    after = before.replace("status: stable", "status: draft").replace(
        "aliases:\n", "- id: new\n  resource: /sources/new.md.source\naliases:\n",
    ).replace("verified:\n", "verified:\n- {by: 'human:forged', at: 2026-09-24T00:00:00Z}\n")
    text, repairs = bookkeeping.apply_bookkeeping(before, after, actor=CURATOR, trusted_now=NOW, stage="curate")
    frontmatter = parse_document(text).frontmatter
    assert repairs == ["restored service-owned verification history without adding verification"]
    assert frontmatter["status"] == "draft"
    assert frontmatter["sources"][-1] == {"id": "new", "resource": "/sources/new.md.source"}
    assert frontmatter["verified"] == parse_document(before).frontmatter["verified"]
    assert f"generated:\n  by: {CURATOR}\n  at: '{STAMP}'\n" in text

    audited, repairs = _audit(before, after, verdict="unverified")
    assert repairs == [
        "restored service-owned verification history",
        "restored service-owned status",
        "restored immutable sources provenance",
    ]
    assert audited == before  # status restored to stable, sources frozen, nothing substantive


def test_new_concept_gets_generation_and_loses_self_verification() -> None:
    new = (
        "---\ntype: Risk\ntitle: New\ndescription: d\ntags: [a]\nstatus: draft\n"
        "sources:\n  - id: s\n    resource: /sources/s.md.source\n"
        f"verified: {{by: {CURATOR}, at: 2026-09-24T00:00:00Z}}\n---\n# New\n"
    )
    text, repairs = bookkeeping.apply_bookkeeping(None, new, actor=CURATOR, trusted_now=NOW, stage="curate")
    assert repairs == ["restored service-owned verification history without adding verification"]
    assert text == new.replace(
        "status: draft\n", f"status: draft\ngenerated: {{by: {CURATOR}, at: {STAMP}}}\n",
    ).replace(f"verified: {{by: {CURATOR}, at: 2026-09-24T00:00:00Z}}\n", "")


def test_deprecated_status_survives_audit_and_draft_becomes_stable() -> None:
    before = _live(ORPHAN_REL)
    assert "status: stable" in _audit(before, before, verdict="unverified")[0]
    deprecated = before.replace("status: draft", "status: deprecated")
    assert "status: deprecated" in _audit(deprecated, deprecated, verdict="unverified")[0]


MAINTAINER = "process:ai-wiki-maintainer"


def _changeset(before: str | None, after: str, **kwargs) -> tuple[str, list[str]]:
    return bookkeeping.apply_bookkeeping(
        before, after, actor=MAINTAINER, trusted_now=NOW, stage="changeset", **kwargs,
    )


def test_changeset_new_concept_starts_draft_with_service_generation_only() -> None:
    forged = (
        "---\ntype: Risk\ntitle: New\ndescription: d\ntags: [a]\nstatus: stable\n"
        "generated: {by: 'human:x', at: 2030-01-01T00:00:00Z}\n"
        "sources:\n  - id: s\n    resource: /sources/s.md.source\n"
        f"verified: {{by: {AUDITOR}, at: 2030-01-01T00:00:00Z}}\n---\n# New\n"
    )
    text, repairs = _changeset(None, forged)
    assert repairs == [
        "restored service-owned verification history without adding verification",
        "restored service-owned status",
    ]
    assert text == (
        "---\ntype: Risk\ntitle: New\ndescription: d\ntags: [a]\nstatus: draft\n"
        f"generated: {{by: '{MAINTAINER}', at: {STAMP}}}\n"
        "sources:\n  - id: s\n    resource: /sources/s.md.source\n---\n# New\n"
    )


def test_changeset_keeps_existing_status_and_history_while_stamping_generation() -> None:
    before = _live("metrics/plugin-install-first-payment-funnel-2026-09.md")
    after = before.replace("status: stable", "status: draft").replace(
        "verified:\n", "verified:\n- {by: 'human:forged', at: 2026-09-24T00:00:00Z}\n",
    ).replace("Redacted fixture body.", "Changed claim.")
    text, repairs = _changeset(before, after)
    assert repairs == [
        "restored service-owned verification history without adding verification",
        "restored service-owned status",
    ]
    frontmatter = parse_document(text).frontmatter
    assert frontmatter["status"] == "stable"
    assert frontmatter["verified"] == parse_document(before).frontmatter["verified"]
    assert frontmatter["generated"] == {"by": MAINTAINER, "at": STAMP}
    assert concept_metadata(frontmatter)["verification_current"] is False
    # Only service keys changed: the file is a no-op and keeps its bytes.
    assert _changeset(before, before.replace("status: stable", "status: draft")) == (
        before, ["restored service-owned status"],
    )


def test_changeset_deprecate_sets_deprecated_status() -> None:
    before = _live("metrics/plugin-install-first-payment-funnel-2026-09.md")
    noted = before.replace("# Summary", "> Deprecated 2026-09-24: superseded by x.\n\n# Summary", 1)
    text, _repairs = _changeset(before, noted, deprecate=True)
    frontmatter = parse_document(text).frontmatter
    assert (frontmatter["status"], frontmatter["generated"]) == ("deprecated", {"by": MAINTAINER, "at": STAMP})
    with pytest.raises(ValueError, match="only a changeset may deprecate"):
        bookkeeping.apply_bookkeeping(before, noted, actor=CURATOR, trusted_now=NOW, stage="curate", deprecate=True)
    with pytest.raises(ValueError, match="require the pre-edit document"):
        _changeset(None, noted, deprecate=True)


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ('"stat\\x75s": stable\n', "status"),  # escaped: the patterns see another name
        ('"s\\x6furces": []\n', "sources"),
        ("!!str status: stable\n", "status"),  # tagged, so it joins the block above
        ("? status\n: stable\n", "status"),  # explicit key
        ("<<: {status: stable}\n", "<<"),  # merge key
    ],
)
def test_changeset_refuses_keys_yaml_reads_under_another_name(line: str, key: str) -> None:
    before = _live("metrics/plugin-install-first-payment-funnel-2026-09.md")
    after = before.replace("aliases:\n", line + "aliases:\n", 1)
    with pytest.raises(bookkeeping.BookkeepingError, match=f"key {key!r} is not written as a plain name") as raised:
        _changeset(before, after)
    assert raised.value.line == after.splitlines().index(line.splitlines()[0]) + 1
    assert _changeset(before, before.replace("Redacted fixture body.", "Changed."))[0]  # plain keys pass
    # The Codex stages keep their behaviour until the changeset path ships.
    assert _audit(before, after)[0]


def test_spill_the_editor_writes_is_discarded_and_reported() -> None:
    """The e5c00b16c75a slip: a reviewer event one line below the closing delimiter."""
    before = _live("metrics/view-references-exposure-proxy-2026-09.md")
    spill = f"  - {{by: {AUDITOR}, at: 2026-09-24T00:59:00Z}}\n"
    end = before.index("\n---\n", 4) + len("\n---\n")
    after = before[:end] + spill + before[end:]
    reported = [f"discarded spilled frontmatter line written by editor: {spill.strip()!r}"]

    text, repairs = _audit(before, after)  # otherwise untouched: the edit is discarded
    assert text == _append(before, SHAPES["metrics/view-references-exposure-proxy-2026-09.md"])
    assert repairs == reported

    text, repairs = _audit(before, after.replace("Redacted fixture body.", "Corrected claim."))
    assert repairs == reported
    assert spill not in text and text.endswith("Corrected claim.\n")

    orphan = _live(ORPHAN_REL)  # a pre-existing orphan is reported as the service's cleanup
    end = orphan.index("\n---\n", 4) + len("\n---\n")
    _text, repairs = _audit(orphan, orphan[:end] + spill + orphan[end:])
    assert repairs == reported + [f"removed spilled frontmatter line from body: {ORPHAN_LINE.strip()!r}"]


def test_knowledge_fields_pushed_into_the_body_are_never_dropped() -> None:
    before = _live(ORPHAN_REL)
    # A stray delimiter above `confidence` pushes aliases/confidence/verified into the body.
    stray = before.replace("confidence: medium", "---\nconfidence: medium").replace(
        "Redacted fixture body.", "Corrected claim.",
    )
    for stage, actor, verdict in (("audit", AUDITOR, "verified"), ("curate", CURATOR, None)):
        with pytest.raises(bookkeeping.BookkeepingError, match="spilled into the body: aliases, confidence"):
            bookkeeping.apply_bookkeeping(before, stray, actor=actor, trusted_now=NOW, stage=stage, verdict=verdict)
    # A body copy of a key the frontmatter still holds is only a duplicate: it is removed.
    end = before.index("\n---\n", 4) + len("\n---\n")
    duplicate = before[:end] + "confidence: medium\n" + before[end:]
    text, repairs = _audit(before, duplicate, verdict="unverified")
    assert parse_document(text).frontmatter["confidence"] == "medium"
    assert "discarded spilled frontmatter line written by editor: 'confidence: medium'" in repairs
    assert body_spill_errors(parse_document(text).body) == []


def test_unpatchable_edits_raise_instead_of_guessing() -> None:
    before = _live(ORPHAN_REL)
    with pytest.raises(bookkeeping.BookkeepingError, match="invalid YAML frontmatter"):
        _audit(before, before.replace("confidence: medium", "confidence: [medium"))
    with pytest.raises(bookkeeping.BookkeepingError, match="unterminated"):
        _audit(before, before.replace("\n---\n", "\n", 1))
    with pytest.raises(ValueError, match="only an audit"):
        bookkeeping.apply_bookkeeping(before, before, actor=CURATOR, trusted_now=NOW, stage="curate",
                                      verdict="verified")


SNAPSHOT = "sources/evidence-95498733626bce82b436b819c0181bd0a55e99db8c6305c977168b6d16764900.md.source"


def _cite(resource: str) -> str:
    return (
        "---\ntype: Risk\ntitle: t\ndescription: d\ntags: [a]\nstatus: draft\n"
        f"sources:\n  - id: s\n    resource: '{resource}'\n    title: Evidence\n---\n# X\n"
    )


@pytest.mark.parametrize(
    "resource",
    [
        "/" + SNAPSHOT.replace("16764900", "1676490"),  # dropped digit
        "/" + SNAPSHOT.replace("6bce82b4", "6bec82b4"),  # swapped digits
        SNAPSHOT,  # bare path resolves under the concept's directory
        "/sources/inbox/" + SNAPSHOT.split("/")[1],  # cited the ingest inbox
    ],
)
def test_snapshot_reference_normalization_repairs_copy_slips(resource: str) -> None:
    text, repairs = bookkeeping.normalize_snapshot_reference(
        _cite(resource), concept_rel="risks/x.md", snapshot_rel=SNAPSHOT,
        existing={SNAPSHOT, "sources/other-evidence.md.source"},
    )
    assert text == _cite("/" + SNAPSHOT)
    assert repairs == [
        f"normalized sources[0].resource to the current snapshot: {resource!r} -> {'/' + SNAPSHOT!r}"
    ]


@pytest.mark.parametrize(
    ("text", "existing"),
    [
        (_cite("/" + SNAPSHOT), {SNAPSHOT}),  # already correct
        (_cite("/sources/unrelated.md.source"), {SNAPSHOT}),  # not close to the snapshot
        (_cite("/sources/b.md.source"), {"sources/a.md.source", "sources/c.md.source"}),  # ambiguous
        (_cite("/sources/other-evidence.md.source"), {SNAPSHOT, "sources/other-evidence.md.source"}),
        (_cite("https://example.test/" + SNAPSHOT), {SNAPSHOT}),
    ],
)
def test_snapshot_reference_normalization_leaves_ambiguous_or_valid_references(
    text: str, existing: set[str],
) -> None:
    snapshot = SNAPSHOT if SNAPSHOT in existing else "sources/a.md.source"
    assert bookkeeping.normalize_snapshot_reference(
        text, concept_rel="risks/x.md", snapshot_rel=snapshot, existing=existing,
    ) == (text, [])


@pytest.mark.parametrize("rel", sorted(SHAPES))
def test_appended_sources_follow_each_live_list_style(rel: str) -> None:
    before = _live(rel)
    text = bookkeeping.append_sources(before, [{"id": "packet", "resource": "evidence:packet"}])
    start = before.index("sources:\n")
    item = next(line for line in before[start:].splitlines()[1:] if line.lstrip().startswith("- "))
    pad = item[: len(item) - len(item.lstrip())]
    assert text.replace(f"{pad}- {{id: packet, resource: 'evidence:packet'}}\n", "", 1) == before
    assert parse_document(text).frontmatter["sources"][-1] == {"id": "packet", "resource": "evidence:packet"}
    inline = "---\ntype: Risk\nsources: [{id: a, resource: /sources/a.md.source}]\n---\n# X\n"
    assert bookkeeping.append_sources(inline, [{"id": "b", "resource": "/sources/b.md.source"}]) == (
        "---\ntype: Risk\nsources:\n  - {id: a, resource: /sources/a.md.source}\n"
        "  - {id: b, resource: /sources/b.md.source}\n---\n# X\n"
    )


def test_source_resource_rewrite_touches_only_matching_resources() -> None:
    text = _cite("evidence:packet").replace("title: Evidence", "title: 'Evidence'")
    rewritten, indices = bookkeeping.rewrite_source_resource(
        text, placeholder="evidence:packet", resource="/" + SNAPSHOT,
    )
    assert (rewritten, indices) == (_cite("/" + SNAPSHOT).replace("title: Evidence", "title: 'Evidence'"), [0])
    quoted = text.replace("title: 'Evidence'", "title: 'cites evidence:packet'")
    assert bookkeeping.rewrite_source_resource(quoted, placeholder="evidence:packet", resource="/x") == (quoted, [])


LIVE_GIT = os.environ.get("AIWIKI_TEST_BUNDLE_GIT")


@pytest.mark.skipif(not LIVE_GIT, reason="set AIWIKI_TEST_BUNDLE_GIT to a production bundle clone")
def test_live_bundle_head_still_validates_and_replay_cleans_the_real_orphan(tmp_path: Path, capsys) -> None:
    checkout = tmp_path / "bundle"
    subprocess.run(["git", "clone", "-q", LIVE_GIT, str(checkout)], check=True)
    # Production removed the orphan on 2026-09-24, so HEAD no longer has it: pin a358395, which does.
    subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "a358395"], check=True)
    assert validate_main([str(checkout)]) == 0
    assert "WARNING: experiments/web-landing-page-aio-ab.md: body starts with" in capsys.readouterr().err
    assert [finding.split(":", 1)[0] for finding in spill_warnings(checkout)] == [ORPHAN_REL]

    before = subprocess.run(
        ["git", "-C", LIVE_GIT, "show", f"3b5731b:{ORPHAN_REL}"], check=True, capture_output=True, text=True,
    ).stdout
    tail = f"  - {{by: {AUDITOR}, at: 2026-09-17T21:18:46Z}}\n"
    new_event = f"  - {{by: {AUDITOR}, at: {STAMP}}}\n"
    lifted = before.replace(f"{tail}---\n{ORPHAN_LINE}\n", f"{tail}{ORPHAN_LINE}\n{new_event}---\n")
    assert lifted != before
    text, repairs = _audit(before, lifted.replace("status: draft", "status: stable"))
    assert f"removed spilled frontmatter line from body: {ORPHAN_LINE.strip()!r}" in repairs
    frontmatter = _assert_valid_and_current(text)
    assert [str(event["at"]) for event in normalize_verified(frontmatter)] == [
        "2026-09-14 20:50:19+00:00", "2026-09-17 21:18:46+00:00", "2026-09-24 01:02:03+00:00",
    ]
    assert yaml.safe_load(text.split("---\n")[1])["generated"]["by"] == CURATOR
