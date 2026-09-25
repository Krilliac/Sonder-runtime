"""valgrind memcheck XML (safe_xml) and macOS .ips (bounded_json) readers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sonder_runtime.domain.crash.apple_ips import parse_apple_ips
from sonder_runtime.domain.crash.model import CaptureFormatError
from sonder_runtime.domain.crash.valgrind_xml import parse_valgrind_xml


FIXTURES = Path(__file__).parent / "fixtures" / "crash"


def _kinds(report) -> set[str]:
    return {hint.kind for hint in report.hints}


def test_memcheck_fatal_signal_is_invalid_read_null_deref():
    report = parse_valgrind_xml((FIXTURES / "memcheck.xml").read_bytes(), source_label="memcheck.xml")
    exc = report.exception
    assert exc.name == "SIGSEGV" and exc.access == "read" and exc.access_address == 0
    top = report.crashing_thread().frames[0]
    assert top.function == "run_frame(Renderer*, int)" and top.file == "/src/sparkmini/crasher.cpp"
    assert top.line == 20 and top.in_project
    assert "null_deref" in _kinds(report)
    assert any("Invalid read of size 8" in note for note in report.notes)
    assert report.process_name == "crasher" and report.pid is not None


def test_memcheck_invalid_read_after_free():
    report = parse_valgrind_xml((FIXTURES / "memcheck_uaf.xml").read_bytes())
    assert report.exception.name == "InvalidRead" and report.exception.access == "read"
    assert report.crashing_thread().frames[0].line == 28
    assert report.threads[1].frames[1].line == 27
    assert {"invalid_read", "use_after_free"} <= _kinds(report)


@pytest.mark.parametrize("payload", [
    b"<?xml version='1.0'?><!DOCTYPE v [<!ENTITY x 'y'>]><valgrindoutput>&x;</valgrindoutput>",
    b"<valgrindoutput><error>",
    b"<other/>",
    b"<valgrindoutput><pid>1</pid></valgrindoutput>",
])
def test_memcheck_refusals(payload):
    with pytest.raises(CaptureFormatError) as info:
        parse_valgrind_xml(payload)
    assert info.value.code == "NOT_VALGRIND"


def test_ips_report():
    report = parse_apple_ips((FIXTURES / "sample.ips").read_text(), source_label="sample.ips")
    assert report.source_kind == "apple_ips" and report.cpu == "ARM-64" and report.os == "macOS 14.3"
    exc = report.exception
    assert exc.name == "EXC_BAD_ACCESS" and exc.signal == "SIGSEGV" and exc.access_address == 8
    crashing = report.crashing_thread()
    assert crashing.thread_id == 5001 and crashing.name == "MainThread"
    assert crashing.frames[0].function == "Renderer::Submit(int)" and crashing.frames[0].line == 42
    assert crashing.frames[0].module == "spark_game" and crashing.frames[0].in_project
    assert not crashing.frames[2].in_project  # dyld
    assert "null_deref" in _kinds(report)
    assert report.signature_basis == "functions"


def test_ips_hostile_json_refused():
    bomb = '{"a":1}\n' + "[" * 10_000 + "]" * 10_000
    with pytest.raises(CaptureFormatError):
        parse_apple_ips(bomb)
    digits = '{"a":1}\n{"pid": ' + "9" * 10_000_000 + "}"
    with pytest.raises(CaptureFormatError):
        parse_apple_ips(digits)
    with pytest.raises(CaptureFormatError) as info:
        parse_apple_ips('{"a": 1}\n{"b": 2}')
    assert info.value.code == "NOT_IPS"


def test_ips_type_confusion_is_tolerated():
    body = {"threads": [{"frames": "nope"}, 7, {"triggered": "yes", "frames": [{"imageIndex": 99}]}],
            "usedImages": {"x": 1}, "exception": {"type": 5, "subtype": ["x"]}, "pid": "12"}
    report = parse_apple_ips("{}\n" + json.dumps(body))
    assert report.pid == 12 and report.threads
