"""The guards that stop a codegen loop from converging on deleted code.

Each test pins a failure that was measured, not imagined: see codegen_loop's
module docstring for where each number came from.
"""
import codegen_loop as cg

import pytest


def test_shrink_rejects_an_amputation():
    """44% and 13% were real repair responses to two- and one-character fixes."""
    original = "x" * 1000
    assert cg.shrink_rejected(original, "y" * 440) is True
    assert cg.shrink_rejected(original, "y" * 130) is True


def test_shrink_allows_a_real_fix():
    """Fixing a syntax error barely changes a file's length."""
    original = "x" * 1000
    assert cg.shrink_rejected(original, "y" * 1002) is False
    assert cg.shrink_rejected(original, "y" * 800) is False


def test_shrink_rejects_a_near_empty_response():
    assert cg.shrink_rejected("x" * 1000, "   ") is True


def test_shrink_allows_anything_when_there_is_no_incumbent():
    """A first generation has nothing to shrink from."""
    assert cg.shrink_rejected("", "a" * 50) is False


def test_errors_are_deduplicated_and_ordered():
    out = "a.cs(1,1): error CS1: x\nfine\na.cs(1,1): error CS1: x\nb.cs(2,2): error CS2: y"
    errors = cg.count_errors(out)
    assert len(errors) == 2
    assert "CS1" in errors[0] and "CS2" in errors[1]


def test_error_regex_can_be_narrowed():
    out = "warning: error-prone thing\nsrc.rs:1: error[E0308]: mismatch"
    assert len(cg.count_errors(out, r"error\[")) == 1


def test_strip_code_takes_the_fenced_block():
    assert cg.strip_code("blah\n```csharp\nint x = 1;\n```\ntrailing").strip() == "int x = 1;"


def test_strip_code_drops_reasoning_blocks():
    """A reasoning model emits <think> inline when not asked for it separately."""
    got = cg.strip_code("<think>let me consider</think>\nint x = 1;\n")
    assert "think" not in got
    assert "int x = 1;" in got


def test_api_extraction_reports_declarations_not_bodies():
    src = (
        "public sealed class Foo\n"
        "{\n"
        "    public int Bar;\n"
        "    public void Baz(int n)\n"
        "    {\n"
        "        int local = n + 1;\n"
        "    }\n"
        "}\n"
    )
    api = cg.extract_api(src)
    assert "class Foo" in api
    assert "public int Bar" in api
    assert "public void Baz(int n)" in api
    assert "local" not in api


def test_dependency_brief_states_the_api_is_real():
    brief = cg.dependency_brief({"a.cs": "public class A\n    public int X;\n"})
    assert "ALREADY EXIST" in brief
    assert "public class A" in brief


def test_dependency_brief_is_empty_without_sources():
    assert cg.dependency_brief({}) == ""
    assert cg.dependency_brief({"a.cs": "   "}) == ""


def test_slips_rewrite_wrong_library_calls():
    text, hits = cg.apply_slips(
        "var c = Color.FromArgb(1,2,3,4);",
        [(r"\bColor\.FromArgb\(", "new Color(")],
    )
    assert hits == 1
    assert "new Color(1,2,3,4)" in text


def test_bad_slip_pattern_is_rejected_not_swallowed():
    with pytest.raises(ValueError):
        cg.parse_slips('[["([unclosed", "x"]]')


def test_slips_must_be_pairs():
    with pytest.raises(ValueError):
        cg.parse_slips('[["only-one"]]')


def test_files_accept_both_shapes():
    assert cg.parse_files('{"a.cs": "spec a"}') == [("a.cs", "spec a")]
    assert cg.parse_files('[{"name": "a.cs", "spec": "spec a"}]') == [("a.cs", "spec a")]


def test_files_json_must_be_json():
    with pytest.raises(ValueError):
        cg.parse_files("not json")


def test_report_warns_that_a_green_build_proves_little():
    """The declared-but-never-assigned field that started all this is not a
    compile error, so success must not be reported as proof of correctness."""
    report = cg.format_report([{"name": "a.cs", "note": "kept"}], [], ok=True)
    assert "BUILD SUCCEEDED" in report
    assert "not proof" in report


def test_report_lists_failures():
    report = cg.format_report([], ["a.cs(1,1): error CS1: boom"], ok=False)
    assert "BUILD FAILED" in report
    assert "boom" in report
