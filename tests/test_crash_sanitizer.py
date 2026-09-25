"""Sanitizer report parsing over logs recorded on this host plus MSVC/HWASan shapes."""
from __future__ import annotations

from pathlib import Path

import pytest

from sonder_runtime.domain.crash.model import CaptureFormatError
from sonder_runtime.domain.crash.sanitizer import parse_sanitizer_report


FIXTURES = Path(__file__).parent / "fixtures" / "crash"


def _parse(name: str):
    return parse_sanitizer_report((FIXTURES / name).read_text(), source_label=name)


def _kinds(report) -> set[str]:
    return {hint.kind for hint in report.hints}


def test_asan_heap_use_after_free_with_free_and_alloc_stacks():
    report = _parse("asan_uaf.log")
    exc = report.exception
    assert report.source_kind == "sanitizer_report"
    assert exc.code == "AddressSanitizer" and exc.name == "heap-use-after-free"
    assert exc.access == "read" and exc.access_address == 0x502000000014 and exc.detail == "READ of size 4"
    top = report.crashing_thread().frames[0]
    assert (top.function, top.file.rsplit("/", 1)[-1], top.line, top.in_project) == \
        ("use_after_free()", "crasher.cpp", 28, True)
    names = [thread.name for thread in report.threads[1:]]
    assert names == ["freed by thread T0 here", "previously allocated by thread T0 here"]
    assert report.threads[1].frames[1].line == 27 and report.threads[2].frames[1].line == 25
    assert not report.threads[1].frames[0].in_project  # the sanitizer's operator delete[]
    assert "use_after_free" in _kinds(report)
    assert report.pid == 26974 and report.process_name == "crasher_asan"
    assert report.signature_basis == "functions"


def test_asan_heap_buffer_overflow():
    report = _parse("asan_overflow.log")
    assert report.exception.name == "heap-buffer-overflow"
    top = report.crashing_thread().frames[0]
    assert top.function == "heap_overflow(int)" and top.line == 33
    assert "heap_buffer_overflow" in _kinds(report)


def test_asan_segv_is_null_deref():
    report = _parse("asan_segv.log")
    assert report.exception.name == "SEGV" and report.exception.signal == "SIGSEGV"
    assert report.exception.access == "read" and report.exception.access_address == 0
    assert "null_deref" in _kinds(report)
    assert report.crashing_thread().frames[0].function == "run_frame(Renderer*, int)"


def test_lsan_leak():
    report = _parse("lsan_leak.log")
    assert report.exception.name == "memory-leak"
    assert [f.line for f in report.crashing_thread().frames if f.in_project] == [57]
    assert "memory_leak" in _kinds(report)


def test_ubsan_with_stack():
    report = _parse("ubsan.log")
    assert report.exception.name == "undefined-behavior"
    assert "signed integer overflow" in report.exception.detail
    top = report.crashing_thread().frames[0]
    assert top.function == "int_overflow(int)" and top.line == 40
    assert "undefined_behavior" in _kinds(report)


def test_ubsan_without_stack_uses_error_location():
    report = parse_sanitizer_report("src/a.cpp:10:5: runtime error: division by zero\n")
    top = report.crashing_thread().frames[0]
    assert (top.file, top.line, top.column) == ("src/a.cpp", 10, 5)


def test_tsan_data_race_two_stacks():
    report = _parse("tsan.log")
    assert report.exception.code == "ThreadSanitizer" and report.exception.name == "data-race"
    assert report.crashing_thread().frames[0].function == "main"
    previous = report.threads[1]
    assert previous.name.startswith("Previous write of size 4") and previous.thread_id == 1
    assert previous.frames[0].function == "bump()"
    assert "data_race" in _kinds(report)


MSVC_ASAN = r"""=================================================================
==11020==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x00e4a37ff5b8 at pc 0x7ff6d6ea1234 bp 0x00e4a37ff4f0 sp 0x00e4a37ff4f8
WRITE of size 4 at 0x00e4a37ff5b8 thread T0
    #0 0x7ff6d6ea1233 in Physics::Integrate C:\agent\_work\3\s\Engine\Physics\integrate.cpp:57
    #1 0x7ff6d6ea2100 in Game::RunFrame C:\agent\_work\3\s\Engine\Game\game.cpp:121
    #2 0x7ff6d6ea4f3b in invoke_main D:\a\_work\1\s\src\vctools\crt\vcstartup\src\startup\exe_common.inl:78
    #3 0x7ffb2f567373 in BaseThreadInitThunk+0x13 (C:\WINDOWS\System32\KERNEL32.DLL+0x180017373)

SUMMARY: AddressSanitizer: stack-buffer-overflow C:\agent\_work\3\s\Engine\Physics\integrate.cpp:57 in Physics::Integrate
"""


def test_msvc_asan_windows_paths():
    report = parse_sanitizer_report(MSVC_ASAN)
    assert report.exception.name == "stack-buffer-overflow" and report.exception.access == "write"
    frames = report.crashing_thread().frames
    assert frames[0].file.endswith("integrate.cpp") and frames[0].line == 57 and frames[0].in_project
    assert frames[3].module == "KERNEL32.DLL" and frames[3].module_offset == 0x180017373
    assert not frames[3].in_project
    assert "stack_buffer_overflow" in _kinds(report)


HWASAN = """==4242==ERROR: HWAddressSanitizer: tag-mismatch on address 0x004e00000010 at pc 0x0055000a1234
READ of size 8 at 0x004e00000010 tags: 2a/7b (ptr/mem) in thread T0
    #0 0x55000a1234 in Renderer::Submit(int) /src/sparkmini/render.cpp:42:9
"""


def test_hwasan_tag_mismatch():
    report = parse_sanitizer_report(HWASAN)
    assert report.exception.code == "HWAddressSanitizer" and report.exception.name == "tag-mismatch"
    assert report.crashing_thread().frames[0].line == 42


def test_hostile_text_is_cleaned_and_bounded():
    text = "==1==ERROR: AddressSanitizer: heap-use-after-free on address 0x10\x1b[31m\n"
    text += "".join("    #%d 0x1 in f%d /src/a.cpp:%d\n" % (i, i, i + 1) for i in range(500))
    report = parse_sanitizer_report(text)
    frames = report.crashing_thread().frames
    assert len(frames) == 64 and report.truncated
    assert "\x1b" not in report.exception.name


def test_second_report_is_noted_not_parsed():
    text = (FIXTURES / "asan_segv.log").read_text() + (FIXTURES / "asan_uaf.log").read_text()
    report = parse_sanitizer_report(text)
    assert report.exception.name == "SEGV"
    assert any("further sanitizer reports" in note for note in report.notes)


def test_not_a_sanitizer_report():
    with pytest.raises(CaptureFormatError) as info:
        parse_sanitizer_report("hello\nworld\n")
    assert info.value.code == "NOT_SANITIZER"
