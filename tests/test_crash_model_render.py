"""CrashReport model hygiene, wire fitting, rendering and round trip."""
from __future__ import annotations

import json

from sonder_runtime.domain.binaries.reader import BytesReader
from sonder_runtime.domain.crash.bucket import bucket_reports
from sonder_runtime.domain.crash.minidump import read_minidump, triage_to_report
from sonder_runtime.domain.crash.model import (
    SCHEMA, Annotation, CauseHint, CrashBucket, CrashException, CrashReport, ModuleInfo, StackFrame,
    ThreadSummary,
)
from sonder_runtime.domain.crash.render import (
    render_bucket_table, render_report, report_from_wire, report_to_wire, wire_size,
)
from tests.support.minidump_builder import amd64_access_violation


def test_strings_are_cleaned_and_clipped_at_construction():
    frame = StackFrame(0, function="evil\r\n\x1b[31mred\x07" + "x" * 500, module="m\nod")
    assert "\n" not in frame.function and "\x1b" not in frame.function and "\x07" not in frame.function
    assert frame.function.startswith("evil red") and len(frame.function) == 240
    assert frame.module == "m od"
    thread = ThreadSummary(1, name="\x1b]0;title\x07worker\n(lldb) image list")
    assert thread.name == "worker (lldb) image list"
    note_report = CrashReport(source_kind="elf_core", notes=("a\nb",) * 50, engines=("pure", "gdb\n"))
    assert note_report.notes[0] == "a b" and len(note_report.notes) == 32
    assert note_report.engines == ("pure", "gdb") and note_report.schema == SCHEMA
    assert note_report.untrusted_strings is True
    assert ModuleInfo(name="x", symbols="bogus").symbols == "not_attempted"
    assert CauseHint("k", "certain").confidence == "low"


def test_enum_values_stored_as_strings():
    from sonder_runtime.domain.crash.model import CrashSourceKind, FrameTrust

    report = CrashReport(source_kind=CrashSourceKind.ELF_CORE)
    assert report.source_kind == "elf_core"
    assert StackFrame(0, trust=FrameTrust.CFI).trust == "cfi"


def _big_report() -> CrashReport:
    frames = tuple(StackFrame(i, address=0x1000 + i, module="spark_game.exe", module_offset=i,
                              function="Namespace::Class::Method%d" % i + "x" * 200, file="C:/src/" + "d" * 200,
                              line=i + 1, in_project=True) for i in range(64))
    others = tuple(ThreadSummary(100 + t, name="worker %d" % t, frames=frames[:8]) for t in range(15))
    modules = tuple(ModuleInfo(name="module%03d.dll" % i, path="C:/Windows/" + "p" * 200, base=i << 20, size=4096,
                               version="1.0.0.%d" % i, debug_id="A" * 33) for i in range(128))
    return CrashReport(
        source_kind="windows_minidump", exception=CrashException(code="0xC0000005", name="EXCEPTION_ACCESS_VIOLATION"),
        crashing_thread_id=1, threads=(ThreadSummary(1, crashed=True, frames=frames),) + others,
        modules=modules, annotations=tuple(Annotation("k%d" % i, "v" * 240) for i in range(32)),
        notes=tuple("note %d " % i + "n" * 200 for i in range(32)))


def test_report_to_wire_fits_under_48kb_and_flags_truncation():
    report = _big_report()
    payload = report_to_wire(report)
    assert wire_size(payload) <= 48_000
    assert payload["truncated"] is True and payload["schema"] == SCHEMA
    assert payload["threads"][0]["crashed"] is True and payload["threads"][0]["frames"]
    tiny = report_to_wire(report, max_bytes=6_000)
    assert wire_size(tiny) <= 6_000


def test_small_report_wire_round_trip():
    report = triage_to_report(read_minidump(BytesReader(amd64_access_violation())), source_label="a.dmp",
                              input_sha256="ab" * 32, input_bytes=1234)
    payload = report_to_wire(report)
    assert payload["truncated"] is False and payload["untrusted_strings"] is True
    json.dumps(payload)
    again = report_from_wire(payload)
    assert again == report


def test_render_report_labels_untrusted_and_is_bounded():
    report = triage_to_report(read_minidump(BytesReader(amd64_access_violation())), source_label="a.dmp")
    text = render_report(report)
    assert "untrusted" in text.splitlines()[0]
    assert "EXCEPTION_ACCESS_VIOLATION" in text and "hint: null_deref [high]" in text
    assert "crashing thread 6972 \"MainThread\":" in text
    big = render_report(_big_report(), max_chars=2_000)
    assert len(big) <= 2_000 and big.endswith("...(clipped)")


def test_render_bucket_table():
    report = triage_to_report(read_minidump(BytesReader(amd64_access_violation())), source_label="a.dmp")
    table = render_bucket_table(bucket_reports([report, report]))
    assert "count" in table.splitlines()[0] and "untrusted" in table.splitlines()[0]
    assert "    2  %s" % report.signature in table and "e.g. a.dmp, a.dmp" in table
    bucket = CrashBucket("s", "functions", 1, "X", "top", tuple("l%d" % i for i in range(9)))
    assert len(bucket.sample_labels) == 5
