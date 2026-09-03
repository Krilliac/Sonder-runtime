"""Boundary tests for sonder_runtime.domain.loop_result_formatting."""

import server
from sonder_runtime.domain.loop_result_formatting import loop_text_result


def test_root_helper_is_identity_preserving_alias():
    assert server._loop_text_result is loop_text_result


def test_ok_result():
    result = loop_text_result("run", "Hello world\nSecond line")
    assert result == {
        "ok": True,
        "type": "run",
        "summary": "Hello world",
        "output": "Hello world\nSecond line",
    }


def test_error_result():
    result = loop_text_result("run", "ERROR: something broke")
    assert result["ok"] is False
    assert result["summary"] == "ERROR: something broke"


def test_empty_text():
    result = loop_text_result("check", "")
    assert result["ok"] is True
    assert result["summary"] == ""
    assert result["output"] == ""


def test_none_text():
    result = loop_text_result("check", None)
    assert result["ok"] is True
    assert result["output"] == ""


def test_summary_truncated():
    long_line = "x" * 300
    result = loop_text_result("run", long_line)
    assert len(result["summary"]) == 200


def test_skips_blank_lines_for_summary():
    result = loop_text_result("run", "\n\n  \nActual content\n")
    assert result["summary"] == "Actual content"
