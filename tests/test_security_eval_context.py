"""The retrieval-context helper: identifiers, classification, and the confined retrieval."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.security_eval.repo_reader import ConfinedRepo, RepoLimits, RetrievalSettings
from sonder_runtime.domain.security_eval.context_select import (
    ContextHit, classify_hit, enclosing_declaration, extract_identifiers, is_guard_line, pack_context)
from sonder_runtime.domain.security_eval.records import LineRange
from tests.support.security_eval_fixtures import CS_SOURCE, PY_CALLER, build_repo

pytestmark = pytest.mark.unit

CS_LINES = CS_SOURCE.splitlines()
PY_LINES = PY_CALLER.splitlines()


def open_repo(root, tmp_path, **settings):
    return ConfinedRepo(str(root), [str(tmp_path)], RepoLimits(), RetrievalSettings(**settings))


@pytest.fixture
def repo_root(tmp_path):
    return build_repo(tmp_path / "repo")


def test_extract_identifiers_drops_keywords_short_and_numeric_and_orders_by_frequency():
    text = "var buffer = new byte[length]; System.Array.Copy(data, buffer, length); abc1 = 0xFF + 12;"
    assert extract_identifiers(text, 12) == ("buffer", "length", "System", "Array", "Copy", "data", "abc1")
    assert extract_identifiers(text, 2) == ("buffer", "length")


def test_enclosing_declaration_finds_the_method_or_function():
    assert enclosing_declaration(CS_LINES, 13) == ("ReadBlock", 7)
    assert enclosing_declaration(CS_LINES, 21) == ("ReadUnchecked", 18)
    assert enclosing_declaration(PY_LINES, 8) == ("handle", 4)
    assert enclosing_declaration(CS_LINES, 1) is None


@pytest.mark.parametrize("line,ident,kind", [
    ("        public byte[] ReadBlock(byte[] data, int length)", "ReadBlock", "definition"),
    ("def handle(packet):", "handle", "definition"),
    ("    public class PacketReader", "PacketReader", "definition"),
    ("    return ReadBlock(packet, size)", "ReadBlock", "caller"),
    ("        if (length > Limit)", "length", "guard"),
    ("            throw new System.ArgumentException(\"too long\");", "System", "guard"),
    ("            var buffer = new byte[length];", "length", "other"),
    ("from reader import ReadBlock", "ReadBlock", "other"),
])
def test_classify_hit(line, ident, kind):
    assert classify_hit(line, ident) == kind


def test_is_guard_line():
    assert is_guard_line("if (x > y)") and is_guard_line("    assert size < 4") and is_guard_line("Validate(x);")
    assert not is_guard_line("var buffer = new byte[length];")


def test_pack_context_orders_budgets_and_flags_truncation():
    hits = [ContextHit("guard", "b.cs", 9, "len", "if (len > 3)"),
            ContextHit("definition", "z.cs", 2, "Read", "int Read(int len)"),
            ContextHit("caller", "a.cs", 5, "Read", "Read(4);"),
            ContextHit("definition", "a.cs", 1, "Read", "int Read(int len) {")]
    bundle = pack_context(hits, 10_000, 3, {"too_common": ("x",)})
    assert [(h.kind, h.path) for h in bundle.items] == [
        ("definition", "a.cs"), ("definition", "z.cs"), ("caller", "a.cs"), ("guard", "b.cs")]
    assert not bundle.truncated and bundle.skipped["too_common"] == ("x",)
    limited = pack_context(hits, 10_000, 1, None)
    assert [h.path for h in limited.items if h.kind == "definition"] == ["a.cs"]
    tight = pack_context(hits, 60, 3, None)
    assert tight.truncated and len(tight.text) <= 60
    assert pack_context([], 100, 3, None).reason


def test_retrieval_finds_definition_caller_and_guard(repo_root, tmp_path):
    bundle = open_repo(repo_root, tmp_path).retrieve("src/PacketReader.cs", LineRange(13, 14))
    found = {(hit.kind, hit.path, hit.line) for hit in bundle.items}
    assert ("definition", "src/PacketReader.cs", 7) in found
    assert ("caller", "tools/caller.py", 8) in found
    assert ("guard", "src/PacketReader.cs", 9) in found
    assert "ReadBlock" in bundle.text and "if (length > Limit)" in bundle.text
    assert not any(hit.path == "src/PacketReader.cs" and 13 <= hit.line <= 14 for hit in bundle.items)
    assert not bundle.truncated


def test_retrieval_respects_the_character_budget(repo_root, tmp_path):
    bundle = open_repo(repo_root, tmp_path, max_context_chars=200).retrieve("src/PacketReader.cs", LineRange(13, 14))
    assert len(bundle.text) <= 200 and bundle.truncated


def test_too_common_identifiers_are_listed_as_skipped(repo_root, tmp_path):
    bundle = open_repo(repo_root, tmp_path, common_hit_limit=3).retrieve("src/PacketReader.cs", LineRange(13, 14))
    assert "length" in bundle.skipped["too_common"]
    assert ("caller", "tools/caller.py", 8) in {(h.kind, h.path, h.line) for h in bundle.items}


def test_no_hits_gives_an_empty_bundle_with_a_reason(repo_root, tmp_path):
    bundle = open_repo(repo_root, tmp_path).retrieve("nested/deep/note.txt", LineRange(1, 1))
    assert bundle.items == () and bundle.text == "" and bundle.reason


def test_output_is_identical_across_runs_and_instances(repo_root, tmp_path):
    first = open_repo(repo_root, tmp_path)
    one = first.retrieve("src/PacketReader.cs", LineRange(13, 14))
    two = first.retrieve("src/PacketReader.cs", LineRange(13, 14))
    three = open_repo(repo_root, tmp_path).retrieve("src/PacketReader.cs", LineRange(13, 14))
    assert one.text == two.text == three.text and one.as_dict() == three.as_dict()
