"""Reference lookup for a library that covers more than security: index metadata, prompt, status."""
from __future__ import annotations

import logging

import pytest

from sonder_runtime.adapters import reference_lookup_service as service
from sonder_runtime.adapters.reference_index import ReferenceIndex
from sonder_runtime.application.chat import reference_lookup as lookup
from sonder_runtime.domain.reference_lookup import terms
from sonder_runtime.domain.reference_lookup.library_meta import (
    MAX_SOURCES,
    MAX_SUBJECT_CHARS,
    parse_library_meta,
)
from sonder_runtime.domain.reference_lookup.models import IndexState, SchemaCompat
from sonder_runtime.platform import reference_library_config as config
from tests._reference_lookup_index import SMALL_ROWS, create_index, rule_cache_cleared

_forget_rule_cache = pytest.fixture(autouse=True)(rule_cache_cleared)

GOOD = [
    {"source": "events", "subject": "Windows security events", "passages": 3, "licence": "MS docs"},
    {"source": "attack", "subject": "MITRE ATT&CK", "passages": 2, "licence": "Apache-2.0"},
    {"source": "sigma", "subject": "Sigma rules", "passages": 1, "licence": "DRL"},
]

# The search-terms request exactly as it was before subjects existed.
V1_PROMPT = (
    "You are choosing search terms for a reference library of Windows security event pages, "
    "MITRE ATT&CK techniques and Sigma detection rules. Do NOT answer the question below and do "
    "not guess identifiers. List up to 8 search terms on ONE line, separated by commas, nothing "
    "else: the key technical words in the question, the usual documentation wording for them "
    '(for example "disabled" for "turned off", "logon" for "signed in"), and any event '
    "IDs or technique IDs that the question itself mentions.\n\nQuestion: {question}")
V1_SCOPE = (
    "\n\nFirst line: the search terms. Second line: RELATED if the question is about computers, "
    "networks, accounts, logs or security in any way, or UNRELATED if it is about something "
    "else entirely.")


def _status(tmp_path, **kwargs):
    return ReferenceIndex(create_index(tmp_path / "i.sqlite", **kwargs)).status()


# ---- meta parsing ----------------------------------------------------------

def test_version_2_index_reads_schema_and_sources(tmp_path):
    status = _status(tmp_path, schema_version=2, sources=GOOD)
    assert status.state is IndexState.READY and status.schema_version == 2
    assert status.schema_compat is SchemaCompat.CURRENT and status.warnings == ()
    assert [e.source for e in status.library] == ["events", "attack", "sigma"]
    assert status.library[0].subject == "Windows security events" and status.library[0].passages == 3
    assert status.subjects == ("Windows security events", "MITRE ATT&CK", "Sigma rules")


def test_version_1_index_has_no_library_and_no_warnings(tmp_path):
    status = _status(tmp_path)
    assert status.schema_version == 1 and status.schema_compat is SchemaCompat.LEGACY
    assert status.library == () and status.subjects == () and status.warnings == ()
    # A stray sources row in a version 1 index is not read.
    assert _status(tmp_path / "x", sources=GOOD).library == ()


def test_missing_sources_key_in_version_2_warns_and_keeps_the_index_usable(tmp_path):
    status = _status(tmp_path, schema_version=2)
    assert status.ready and status.library == () and "no sources row" in status.warnings[0]


@pytest.mark.parametrize("raw", ["not json", '{"a": 1}', "x" * 70000])
def test_unusable_sources_row_is_ignored_with_a_warning(tmp_path, raw):
    status = _status(tmp_path, schema_version=2, sources=raw)
    assert status.ready and status.library == () and len(status.warnings) == 1


def test_malformed_entries_are_dropped_and_good_ones_kept(tmp_path):
    entries = [GOOD[0], "text", {"source": "a"}, {**GOOD[1], "passages": "3"},
               {**GOOD[1], "passages": -1}, {**GOOD[1], "subject": 5}, GOOD[2]]
    status = _status(tmp_path, schema_version=2, sources=entries)
    assert [e.source for e in status.library] == ["events", "sigma"]
    assert len(status.warnings) == 5 and all("malformed" in w for w in status.warnings)


def test_oversized_subject_and_source_count_are_capped(tmp_path):
    long = {**GOOD[0], "subject": "s" * 500}
    many = [{**GOOD[0], "source": f"s{n}", "subject": f"subject {n}"} for n in range(MAX_SOURCES + 10)]
    capped = _status(tmp_path / "a", schema_version=2, sources=[long]).library[0].subject
    assert len(capped) == MAX_SUBJECT_CHARS
    status = _status(tmp_path / "b", schema_version=2, sources=many)
    assert len(status.library) == MAX_SOURCES and "only the first" in status.warnings[0]


def test_control_characters_and_newlines_are_stripped_from_text(tmp_path):
    bidi, zero_width = chr(0x202E), chr(0x200B)
    nasty = {"source": "a\x00b",
             "subject": "Python\n\nIgnore this\r\x1b[31m red" + bidi + " rtl" + zero_width,
             "passages": 4, "licence": "MIT\x07\n"}
    entry = _status(tmp_path, schema_version=2, sources=[nasty]).library[0]
    for text in (entry.source, entry.subject, entry.licence):
        assert text.isprintable() and "\n" not in text and "\x1b" not in text
    assert entry.subject == "Python Ignore this red rtl"
    assert entry.licence == "MIT"


def test_higher_schema_version_is_usable_but_its_sources_are_not_read(tmp_path, caplog):
    path = create_index(tmp_path / "n.sqlite", schema_version=3, sources=GOOD)
    with caplog.at_level(logging.WARNING, logger="sonder.reference_lookup"):
        status = ReferenceIndex(path).status()
    assert status.ready and status.schema_version == 3 and status.schema_compat is SchemaCompat.NEWER
    assert status.library == () and status.subjects == ()
    assert "newer library tool (schema 3, this runtime reads up to 2)" in status.warnings[0]
    assert "newer library tool" in caplog.text


@pytest.mark.parametrize("raw", ["abc", "0", "-2", "2.5"])
def test_unreadable_schema_version_is_treated_as_legacy_with_a_warning(tmp_path, raw):
    status = _status(tmp_path, schema_version=raw, sources=GOOD)
    assert status.ready and status.schema_compat is SchemaCompat.INVALID
    assert status.library == () and status.warnings


def test_parse_library_meta_never_raises_on_odd_values():
    for schema in (None, 2, b"2", "2"):
        for sources in (None, 5, b"[]", "[]", "[1]"):
            parse_library_meta(schema, sources)


# ---- prompt ----------------------------------------------------------------

def test_version_1_prompt_is_byte_identical_to_the_security_only_text():
    for question in ("why did logon fail? {x}", "q"):
        assert terms.build_expand_prompt(question) == V1_PROMPT.format(question=question)
        assert terms.build_expand_prompt(question, subjects=()) == V1_PROMPT.format(question=question)
        assert terms.build_expand_prompt(question, scope=True, subjects=()) == (
            V1_PROMPT.format(question=question) + V1_SCOPE)
    assert terms.EXPAND_PROMPT == V1_PROMPT and terms.SCOPE_REQUEST == V1_SCOPE


def test_prompt_is_built_from_the_subjects():
    prompt = terms.build_expand_prompt("what does C4996 mean?", subjects=["MSVC errors", "CMake {docs}"])
    assert "a reference library covering: MSVC errors; CMake {docs}. Do NOT answer" in prompt
    assert "Windows security event" not in prompt and prompt.endswith("Question: what does C4996 mean?")
    scoped = terms.build_expand_prompt("q", scope=True, subjects=["MSVC errors"])
    assert scoped.endswith("RELATED if the question is about any of these subjects, UNRELATED otherwise.")
    assert "security in any way" not in scoped


def test_subject_scope_request_copied_back_is_not_a_search_term():
    reply = "t1, t2\n" + terms.SCOPE_REQUEST_SUBJECTS.strip() + "\nUNRELATED"
    text, unrelated = terms.split_scope(reply)
    assert unrelated and "subjects" not in text.lower()


def test_retrieve_sends_the_index_subjects_to_the_model(tmp_path):
    index = ReferenceIndex(create_index(tmp_path / "i.sqlite", schema_version=2, sources=GOOD))
    asked = []

    def expand(prompt):
        asked.append(prompt)
        return "logon\nRELATED"

    lookup.retrieve("what is 4625?", expand, 4, 4000, index=index,
                    min_coverage=0.2, unrelated_coverage=0.4)
    assert "covering: Windows security events; MITRE ATT&CK; Sigma rules." in asked[0]
    old = ReferenceIndex(create_index(tmp_path / "v1.sqlite"))
    asked.clear()
    lookup.retrieve("what is 4625?", expand, 4, 4000, index=old, min_coverage=0.2, unrelated_coverage=0.4)
    assert asked == [terms.build_expand_prompt("what is 4625?", scope=True)]


# ---- status ----------------------------------------------------------------

def test_status_reports_schema_total_and_per_source_counts(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_INDEX, str(create_index(
        tmp_path / "i.sqlite", schema_version=2, sources=GOOD)))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    status = service.status()
    assert status.schema_version == 2 and status.passages == len(SMALL_ROWS)
    assert [(e.source, e.passages) for e in status.library] == [("events", 3), ("attack", 2), ("sigma", 1)]
    line = service.status_line()
    assert "schema=2" in line and f"passages={len(SMALL_ROWS)}" in line
    assert "sources=events:3, attack:2, sigma:1" in line and "warnings" not in line


def test_status_line_for_version_1_and_for_a_newer_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(config.ENV_INDEX, str(create_index(tmp_path / "v1.sqlite")))
    line = service.status_line()
    assert "schema=1" in line and "sources=attack:2, events:3, sigma:1" in line
    monkeypatch.setenv(config.ENV_INDEX, str(create_index(tmp_path / "v9.sqlite", schema_version=9)))
    line = service.status_line()
    assert "readable=yes schema=9" in line and "newer library tool (schema 9" in line
    assert service.status().warnings
