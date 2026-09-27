"""Pure triage: the lane A crash readers and lane B profile readers behind one port.

Nothing here launches a process. Binary captures are read through the
``FileByteReader`` the capture source opened (exact-length, budgeted reads);
text captures through a bounded window (8 MiB parse window, UTF-8 with
replacement). Tier-1 debugger text is parsed here too (``merge_crash``), so
the application service stays free of format knowledge: it hands over the
parser name the planner chose, the job output and the run's nonce.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Iterator, Mapping

from ...application.debugging.ports import (
    CAPTURE_FORMAT_UNKNOWN,
    PARSE_FAILED,
    CaptureIdentity,
    ProfileDigestRequest,
    debug_error,
)
from ...domain.binaries.reader import BinaryFormatError
from ...domain.common.errors import InvalidInput, SonderError
from ...domain.crash import debugger_text
from ...domain.crash.bucket import bucket_reports
from ...domain.crash.elf_core import core_to_report, read_elf_core
from ...domain.crash.hints import finalize_report
from ...domain.crash.minidump import read_minidump, triage_to_report
from ...domain.crash.render import merge_findings, report_from_wire, report_to_wire
from ...domain.profiling.model import ProfileFormatUnknown, ProfileParseError, with_source

TEXT_WINDOW_BYTES = 8 << 20
STREAM_CHUNK_BYTES = 1 << 20
CACHE_WIRE_BYTES = 4_000_000
_MINIDUMPS = ("windows_minidump", "breakpad_minidump", "crashpad_minidump")

_CRASH_PARSERS = {
    "cdb": lambda text, nonce: debugger_text.parse_cdb(text, nonce),
    "gdb": lambda text, nonce: debugger_text.parse_gdb(text, nonce),
    "lldb": lambda text, nonce: debugger_text.parse_lldb(text, nonce),
    "eu_stack": lambda text, nonce: debugger_text.parse_eu_stack(text),
    "stackwalk_json": lambda text, nonce: debugger_text.parse_stackwalk_json(text),
    "stackwalk_machine": lambda text, nonce: debugger_text.parse_stackwalk_machine(text),
    "symbolizer_json": lambda text, nonce: debugger_text.parse_symbolizer_json(text),
}


def _parse_failed(exc: Exception) -> SonderError:
    code = getattr(exc, "code", "") or type(exc).__name__
    return debug_error(PARSE_FAILED, "the capture could not be read (%s)" % code)


def _text_window(reader, limit: int = TEXT_WINDOW_BYTES) -> str:
    size = int(reader.size)
    data = reader.read(0, min(size, limit))
    return data.decode("utf-8", "replace")


def _chunks(reader, chunk: int = STREAM_CHUNK_BYTES) -> Iterator[bytes]:
    size = int(reader.size)
    offset = 0
    while offset < size:
        length = min(chunk, size - offset)
        yield reader.read(offset, length)
        offset += length


def _lines(reader) -> Iterator[str]:
    pending = b""
    for block in _chunks(reader):
        pending += block
        *complete, pending = pending.split(b"\n")
        for line in complete:
            yield line.decode("utf-8", "replace")
    if pending:
        yield pending.decode("utf-8", "replace")


def _sha64(value: str) -> str:
    text = str(value or "").lower()
    return text if len(text) == 64 and all(c in "0123456789abcdef" for c in text) else ""


class PureCaptureTriage:
    """``PureTriage`` over the lane A/B readers."""

    # -- crash ---------------------------------------------------------------

    def crash(self, identity: CaptureIdentity, reader):
        kind = identity.kind
        common = {"source_label": identity.label, "input_sha256": identity.sha256,
                  "input_bytes": identity.size}
        try:
            if kind in _MINIDUMPS:
                return triage_to_report(read_minidump(reader), **common)
            if kind == "elf_core":
                return core_to_report(read_elf_core(reader), **common)
            if kind == "sanitizer_report":
                from ...domain.crash.sanitizer import parse_sanitizer_report

                return parse_sanitizer_report(_text_window(reader), **common)
            if kind == "valgrind_xml":
                from ...domain.crash.valgrind_xml import parse_valgrind_xml

                return parse_valgrind_xml(reader.read(0, min(reader.size, TEXT_WINDOW_BYTES)),
                                          **common)
            if kind == "apple_ips":
                from ...domain.crash.apple_ips import parse_apple_ips

                return parse_apple_ips(_text_window(reader), **common)
        except (BinaryFormatError, InvalidInput, ValueError, RecursionError) as exc:
            raise _parse_failed(exc) from None
        raise debug_error(CAPTURE_FORMAT_UNKNOWN, "not a recognised crash capture")

    def bucket(self, reports):
        return bucket_reports(list(reports))

    def merge_crash(self, base, parser: str, text: str, nonce: str, engine: str):
        function = _CRASH_PARSERS.get(parser)
        if function is None:
            return base
        try:
            findings = function(text, nonce)
        except (BinaryFormatError, InvalidInput, ValueError, RecursionError) as exc:
            raise _parse_failed(exc) from None
        return merge_findings(base, findings, engine)

    def finish_crash(self, report, *, engines, module_symbols, egress_isolation, notes, truncated):
        states = {str(name).lower(): str(state) for name, state in module_symbols}
        modules = report.modules
        if states:
            modules = tuple(
                replace(module, symbols=states[module.name.lower()])
                if module.name.lower() in states else module
                for module in report.modules)
        merged_notes = tuple(report.notes) + tuple(n for n in notes if n not in report.notes)
        engine_list = tuple(dict.fromkeys(tuple(report.engines) + tuple(engines)))
        report = replace(report, modules=modules, engines=engine_list,
                         egress_isolation=egress_isolation, notes=merged_notes,
                         truncated=bool(report.truncated or truncated))
        return finalize_report(report)

    def crash_to_wire(self, report) -> dict:
        return report_to_wire(report, max_bytes=CACHE_WIRE_BYTES)

    def crash_from_wire(self, data: Mapping):
        return report_from_wire(data)

    # -- profiles -------------------------------------------------------------

    def profile(self, identity: CaptureIdentity, reader, request: ProfileDigestRequest):
        kind = identity.kind
        top_n = max(5, min(50, int(request.top_n or 25)))
        budget = request.frame_budget_ms
        try:
            if kind == "callgrind":
                from ...domain.profiling.callgrind import parse_callgrind

                digest = parse_callgrind(_lines(reader), top_n=top_n)
            elif kind == "chrome_trace":
                from ...domain.profiling.chrome_trace import parse_chrome_trace

                kwargs = {"frame_budget_ms": budget, "thread": request.thread, "top_n": top_n}
                if request.frame_zone:
                    kwargs["frame_zone"] = request.frame_zone
                digest = parse_chrome_trace(_chunks(reader), **kwargs)
            elif kind == "tracy_csv":
                from ...domain.profiling.tracy_csv import parse_tracy_csv

                digest = parse_tracy_csv(_text_window(reader), frame_zone=request.frame_zone,
                                         frame_budget_ms=budget, thread=request.thread,
                                         top_n=top_n)
            elif kind in ("profile_csv", "wpa_csv", "pix_csv", "superluminal_csv"):
                from ...domain.profiling.tabular_csv import parse_profile_csv

                hint = {"wpa_csv": "wpa", "pix_csv": "pix", "superluminal_csv": "superluminal"}.get(
                    kind, "auto")
                digest = parse_profile_csv(_text_window(reader), hint, frame_zone=request.frame_zone,
                                           frame_budget_ms=budget, thread=request.thread,
                                           top_n=top_n)
            elif kind == "heaptrack_text":
                from ...domain.profiling.heaptrack_text import parse_heaptrack_print

                digest = parse_heaptrack_print(_text_window(reader))
            elif kind == "perf_text":
                digest = self._perf_text(_text_window(reader), top_n)
            else:
                return None
        except ProfileFormatUnknown as exc:
            raise debug_error(CAPTURE_FORMAT_UNKNOWN, str(exc)) from None
        except (ProfileParseError, InvalidInput, ValueError, RecursionError) as exc:
            raise _parse_failed(exc) from None
        return with_source(digest, source_label=identity.label,
                           input_sha256=_sha64(identity.sha256), engines=("pure",))

    @staticmethod
    def _perf_text(text: str, top_n: int):
        from ...domain.profiling.perf_text import parse_perf_flat, parse_perf_folded

        folded = parse_perf_folded(text, top_n=top_n)
        if folded.top_self:
            return folded
        return parse_perf_flat(text, top_n=top_n)

    def profile_from_steps(self, identity_label: str, input_sha256: str, source_kind: str,
                           outputs, request: ProfileDigestRequest, *, engines, egress_isolation,
                           notes, truncated):
        by_parser: dict[str, str] = {}
        for parser, text in outputs:
            by_parser.setdefault(str(parser), str(text))
        top_n = max(5, min(50, int(request.top_n or 25)))
        try:
            digest = self._profile_text(by_parser, request, top_n)
        except ProfileFormatUnknown as exc:
            raise debug_error(CAPTURE_FORMAT_UNKNOWN, str(exc)) from None
        except (ProfileParseError, InvalidInput, ValueError, RecursionError) as exc:
            raise _parse_failed(exc) from None
        if digest is None:
            raise debug_error(PARSE_FAILED, "the host tool produced no readable output")
        digest = with_source(digest, source_label=identity_label, input_sha256=_sha64(input_sha256),
                             engines=tuple(dict.fromkeys(("pure", *engines))),
                             egress_isolation=egress_isolation, extra_notes=tuple(notes))
        if truncated and not digest.truncated:
            digest = replace(digest, truncated=True)
        return digest

    @staticmethod
    def _profile_text(by_parser: Mapping[str, str], request: ProfileDigestRequest, top_n: int):
        if "perf_folded" in by_parser or "perf_flat" in by_parser:
            from ...domain.profiling.perf_text import (
                merge_perf_digests,
                parse_perf_flat,
                parse_perf_folded,
            )

            flat = parse_perf_flat(by_parser["perf_flat"], top_n=top_n) if "perf_flat" in by_parser else None
            if "perf_folded" not in by_parser:
                return flat
            return merge_perf_digests(parse_perf_folded(by_parser["perf_folded"], top_n=top_n), flat)
        if "heaptrack_text" in by_parser:
            from ...domain.profiling.heaptrack_text import parse_heaptrack_print

            return parse_heaptrack_print(by_parser["heaptrack_text"])
        if "tracy_csv" in by_parser or "tracy_csv_unwrap" in by_parser:
            from ...domain.profiling.tracy_csv import parse_tracy_csv

            text = by_parser.get("tracy_csv_unwrap") or by_parser["tracy_csv"]
            return parse_tracy_csv(text, frame_zone=request.frame_zone,
                                   frame_budget_ms=request.frame_budget_ms, thread=request.thread,
                                   top_n=top_n)
        for parser in ("wpa_csv", "xperf_text"):
            if parser in by_parser:
                from ...domain.profiling.tabular_csv import parse_profile_csv

                return parse_profile_csv(by_parser[parser], "wpa" if parser == "wpa_csv" else "auto",
                                         frame_budget_ms=request.frame_budget_ms,
                                         thread=request.thread, top_n=top_n)
        return None

    def profile_to_wire(self, digest) -> dict:
        from ...domain.profiling.render import digest_to_wire

        return digest_to_wire(digest, max_bytes=CACHE_WIRE_BYTES)

    def profile_from_wire(self, data: Mapping):
        from ...domain.profiling.render import digest_from_wire

        return digest_from_wire(data)


__all__ = ["PureCaptureTriage", "TEXT_WINDOW_BYTES"]
