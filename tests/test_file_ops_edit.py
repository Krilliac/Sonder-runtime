import json

import pytest

from sonder_runtime.adapters.filesystem import file_ops


@pytest.fixture
def rooted(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    return project


def edit(rooted, name, old, new, **kwargs):
    return file_ops.edit_file(str(rooted / name), old, new, **kwargs)


def test_lf_old_matches_crlf_and_preserves_crlf(rooted):
    path = rooted / "demo.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = edit(rooted, "demo.txt", "one\ntwo", "uno\ndos")
    assert path.read_bytes() == b"uno\r\ndos\r\nthree\r\n"
    assert result["replacements"] == 1


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_ambiguous_count_one_names_lines_and_leaves_file(rooted, eol):
    path = rooted / "demo.txt"
    original = eol.join(["x = 1", "y = 2", "x = 1", "y = 2", ""])
    path.write_bytes(original.encode())
    with pytest.raises(ValueError, match=r"old text matches 2 places.*lines 1, 3"):
        edit(rooted, "demo.txt", "x = 1\ny = 2", "x = 3\ny = 4")
    assert path.read_bytes() == original.encode()


def test_indentation_fallback_reports_note_and_reindents_new(rooted):
    path = rooted / "demo.py"
    path.write_bytes(b"def outer():\r\n    value = 1\r\n    return value\r\n")
    result = edit(rooted, "demo.py", "  value = 1\n  return value", "value = 2\nreturn value + 1")
    assert path.read_bytes() == b"def outer():\r\n    value = 2\r\n    return value + 1\r\n"
    assert result["note"] == "matched with normalised whitespace"


def test_syntax_report_keeps_invalid_python_edit(rooted):
    path = rooted / "bad.py"
    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    result = edit(rooted, "bad.py", "return 1", "def f(:")
    assert result["syntax"].startswith("SyntaxError line 2:")
    assert "def f(:" in path.read_text(encoding="utf-8")


def test_numbered_echo_is_bounded_and_has_context(rooted):
    path = rooted / "demo.txt"
    path.write_text("\n".join(f"line {i}" for i in range(1, 100)), encoding="utf-8")
    result = edit(rooted, "demo.txt", "line 50", "changed")
    assert f"{50:6d}  changed" in result["text"]
    assert len(result["text"].splitlines()) <= 60


def test_json_report_is_additive(rooted):
    path = rooted / "data.json"
    path.write_text('{"a": 1}\n', encoding="utf-8")
    result = edit(rooted, "data.json", '"a": 1', '"a": 2')
    assert json.loads(path.read_text(encoding="utf-8"))["a"] == 2
    assert result["syntax"] == "ok"


def test_mixed_endings_outside_replacement_are_byte_identical(rooted):
    path = rooted / "mixed.txt"
    path.write_bytes(b"prefix\nold\r\nblock\r\nsuffix\r\nlast\n")
    result = edit(rooted, "mixed.txt", "old\nblock", "new\nblock")
    assert path.read_bytes() == b"prefix\nnew\r\nblock\r\nsuffix\r\nlast\n"
    assert result["replacements"] == 1


def test_rstrip_fallback_with_trailing_newline_preserves_next_line(rooted):
    path = rooted / "spaces.txt"
    path.write_bytes(b"keep\r\n  old  \r\n  block \r\nnext\r\n")
    result = edit(rooted, "spaces.txt", "  old\n  block\n", "  new\n  content\n")
    assert path.read_bytes() == b"keep\r\n  new\r\n  content\r\nnext\r\n"
    assert result["note"] == "matched with normalised whitespace"


def test_exact_tier_wins_over_additional_whitespace_matches(rooted):
    path = rooted / "exact.txt"
    path.write_bytes(b"old\nblock\nold \nblock \n")
    result = edit(rooted, "exact.txt", "old\nblock", "changed")
    assert path.read_bytes() == b"changed\nold \nblock \n"
    assert "note" not in result


def test_explicit_count_replaces_both_and_echoes_both_regions(rooted):
    path = rooted / "both.txt"
    path.write_bytes(b"before\r\nx\r\ny\r\nmiddle\r\nx\r\ny\r\nafter\r\n")
    result = edit(rooted, "both.txt", "x\ny", "changed", count=2)
    assert path.read_bytes() == b"before\r\nchanged\r\nmiddle\r\nchanged\r\nafter\r\n"
    assert result["replacements"] == 2
    assert "     2  changed" in result["text"]
    assert "     4  changed" in result["text"]


def test_whitespace_ambiguity_leaves_file_unchanged(rooted):
    path = rooted / "ambiguous.txt"
    before = b"  old \n  block \n  old\n  block\n"
    path.write_bytes(before)
    with pytest.raises(ValueError, match=r"matches 2 places \(lines 1, 3\)"):
        edit(rooted, "ambiguous.txt", "old\nblock", "new")
    assert path.read_bytes() == before


def test_echo_has_exactly_three_context_lines_and_sixty_line_cap(rooted):
    path = rooted / "echo.txt"
    path.write_bytes("\n".join(f"line {i}" for i in range(1, 101)).encode())
    result = edit(rooted, "echo.txt", "line 50", "changed")
    assert result["text"].splitlines()[0] == "    47  line 47"
    assert result["text"].splitlines()[-1] == "    53  line 53"
    result = edit(rooted, "echo.txt", "changed", "\n".join(["added"] * 100))
    assert len(result["text"].splitlines()) == 60
    assert result["echo_truncated"] is True


def test_bad_json_is_kept_and_flagged(rooted):
    path = rooted / "bad.json"
    path.write_bytes(b'{"key": 1}\n')
    result = edit(rooted, "bad.json", "1", "!")
    assert path.read_bytes() == b'{"key": !}\n'
    assert result["syntax"].startswith("JSONDecodeError line 1:")


def test_utf8_bom_python_is_still_valid(rooted):
    path = rooted / "bom.py"
    path.write_bytes(b"\xef\xbb\xbfvalue = 1\r\n")
    result = edit(rooted, "bom.py", "1", "2")
    assert path.read_bytes() == b"\xef\xbb\xbfvalue = 2\r\n"
    assert result["syntax"] == "ok"


def test_removing_entire_contents_returns_empty_echo(rooted):
    path = rooted / "empty.txt"
    path.write_bytes(b"only line\r\n")
    result = edit(rooted, "empty.txt", "only line\n", "")
    assert path.read_bytes() == b""
    assert result["text"] == ""
    assert result["replacements"] == 1


def test_lf_patch_context_applies_to_crlf(rooted):
    from sonder_runtime.adapters.filesystem import text_patch

    path = rooted / "patch.txt"
    path.write_bytes(b"before\r\nold\r\nafter\r\n")
    patch = "--- a/patch.txt\n+++ b/patch.txt\n@@ -1,3 +1,3 @@\n before\n-old\n+new\n after\n"
    result = text_patch.text_patch(str(rooted), patch, apply=True)
    assert result["applied"] is True
    assert path.read_bytes() == b"before\r\nnew\r\nafter\r\n"
