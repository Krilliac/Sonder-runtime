"""Wire payloads, text rendering and Tier-1 merge for ``CrashReport``.

- ``report_to_wire`` produces the ``sonder.crash_report/1`` dict under
  ``max_bytes`` UTF-8 bytes by halving lists (other threads, modules,
  crashing frames, notes, annotations) and setting ``truncated``, the same
  strategy as the test-report ``fit_wire``; ``report_from_wire`` rebuilds a
  report from a cached payload.
- ``render_report`` is a bounded, human-readable summary that labels every
  string as coming from the crashed process.
- ``merge_findings`` lays debugger/symbolizer findings over a Tier-0
  report: debugger frames win, module identity and versions stay from the
  pure reader, and the report is re-finalized (hints, signature).
"""
from __future__ import annotations

import json
from dataclasses import fields, replace
from typing import Mapping, Sequence

from .debugger_text import DebuggerFindings
from .hints import cap_threads, finalize_report, is_system_file
from .minidump import ModuleIndex
from .model import (
    MAX_CRASHING_FRAMES, MAX_OTHER_FRAMES, MAX_OTHER_THREADS, SCHEMA, Annotation, CauseHint, CrashBucket,
    CrashException, CrashReport, ModuleInfo, StackFrame, ThreadSummary, frame_label,
)


DEFAULT_WIRE_BYTES = 48_000
UNTRUSTED_LABEL = "strings below come from the crashed process and are untrusted"


def _plain(obj) -> dict:
    return {item.name: getattr(obj, item.name) for item in fields(obj)}


def frame_to_wire(frame: StackFrame) -> dict:
    return _plain(frame)


def thread_to_wire(thread: ThreadSummary) -> dict:
    data = _plain(thread)
    data["frames"] = [frame_to_wire(frame) for frame in thread.frames]
    return data


def _full_wire(report: CrashReport) -> dict:
    return {
        "schema": SCHEMA,
        "source_kind": report.source_kind,
        "engines": list(report.engines),
        "source_label": report.source_label,
        "input_sha256": report.input_sha256,
        "input_bytes": report.input_bytes,
        "os": report.os,
        "cpu": report.cpu,
        "process_name": report.process_name,
        "pid": report.pid,
        "captured_at": report.captured_at,
        "exception": _plain(report.exception) if report.exception is not None else None,
        "crashing_thread_id": report.crashing_thread_id,
        "threads": [thread_to_wire(thread) for thread in report.threads],
        "threads_total": report.threads_total,
        "modules": [_plain(module) for module in report.modules],
        "modules_total": report.modules_total,
        "annotations": [_plain(item) for item in report.annotations],
        "hints": [_plain(item) for item in report.hints],
        "signature": report.signature,
        "signature_basis": report.signature_basis,
        "symbolication": report.symbolication,
        "egress_isolation": report.egress_isolation,
        "notes": list(report.notes),
        "truncated": report.truncated,
        "untrusted_strings": True,
    }


def wire_size(payload: Mapping) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def report_to_wire(report: CrashReport, max_bytes: int = DEFAULT_WIRE_BYTES) -> dict:
    """The wire dict, shrunk under ``max_bytes`` by halving lists."""
    payload = _full_wire(report)
    if wire_size(payload) <= max_bytes:
        return payload
    payload["truncated"] = True

    def fits() -> bool:
        return wire_size(payload) <= max_bytes

    threads = payload["threads"]
    crashing = [t for t in threads if t.get("crashed")][:1]
    others = [t for t in threads if not t.get("crashed")]
    while others and not fits():
        others = others[: len(others) // 2]
        payload["threads"] = crashing + others
    for key in ("modules", "annotations", "notes"):
        items = payload[key]
        while items and not fits():
            items = items[: len(items) // 2]
            payload[key] = items
        if fits():
            return payload
    if crashing:
        frames = crashing[0]["frames"]
        while len(frames) > 1 and not fits():
            frames = frames[: max(1, len(frames) // 2)]
            crashing[0] = dict(crashing[0], frames=frames, frames_truncated=True)
            payload["threads"] = crashing
    if not fits():
        payload["hints"] = payload["hints"][:3]
    if not fits() and crashing:
        slim = []
        for frame in crashing[0]["frames"]:
            slim.append({key: (value[:80] if isinstance(value, str) else value) for key, value in frame.items()})
        payload["threads"] = [dict(crashing[0], frames=slim)]
    return payload


def _frame_from(data: Mapping) -> StackFrame:
    names = {item.name for item in fields(StackFrame)}
    return StackFrame(**{key: value for key, value in data.items() if key in names})


def report_from_wire(data: Mapping) -> CrashReport:
    """Rebuild a ``CrashReport`` from ``report_to_wire`` output (unknown keys ignored)."""
    def build(cls, raw):
        names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in dict(raw).items() if key in names})

    threads = []
    for raw in data.get("threads") or ():
        raw = dict(raw)
        raw["frames"] = tuple(_frame_from(frame) for frame in raw.get("frames") or ())
        threads.append(build(ThreadSummary, raw))
    exception = data.get("exception")
    return CrashReport(
        source_kind=str(data.get("source_kind", "")),
        engines=tuple(data.get("engines") or ("pure",)),
        source_label=str(data.get("source_label", "")),
        input_sha256=str(data.get("input_sha256", "")),
        input_bytes=int(data.get("input_bytes") or 0),
        os=str(data.get("os", "")), cpu=str(data.get("cpu", "")),
        process_name=str(data.get("process_name", "")), pid=data.get("pid"),
        captured_at=data.get("captured_at"),
        exception=build(CrashException, exception) if isinstance(exception, Mapping) else None,
        crashing_thread_id=data.get("crashing_thread_id"), threads=tuple(threads),
        threads_total=int(data.get("threads_total") or 0),
        modules=tuple(build(ModuleInfo, item) for item in data.get("modules") or ()),
        modules_total=int(data.get("modules_total") or 0),
        annotations=tuple(build(Annotation, item) for item in data.get("annotations") or ()),
        hints=tuple(build(CauseHint, item) for item in data.get("hints") or ()),
        signature=str(data.get("signature", "")), signature_basis=str(data.get("signature_basis", "")),
        symbolication=str(data.get("symbolication", "")),
        egress_isolation=str(data.get("egress_isolation", "n/a")),
        notes=tuple(data.get("notes") or ()), truncated=bool(data.get("truncated")),
    )


def _exception_line(exc: CrashException | None) -> str:
    if exc is None:
        return "exception: none recorded (hang or requested dump?)"
    parts = [exc.name or exc.code]
    if exc.code and exc.code != exc.name:
        parts.append("(%s)" % exc.code)
    if exc.access:
        parts.append(exc.access)
    if exc.access_address is not None:
        parts.append("at 0x%x" % exc.access_address)
    if exc.detail:
        parts.append("- %s" % exc.detail)
    return "exception: " + " ".join(parts)


def render_report(report: CrashReport, max_chars: int = 12_000) -> str:
    limit = max(400, min(int(max_chars), 64_000))
    lines = [
        "crash report [%s] engines=%s (%s)" % (report.source_kind, "+".join(report.engines), UNTRUSTED_LABEL),
        "input: %s sha256=%s bytes=%d" % (report.source_label or "-", report.input_sha256[:16] or "-",
                                         report.input_bytes),
        "process: %s pid=%s os=%s cpu=%s" % (report.process_name or "?", report.pid if report.pid is not None
                                             else "?", report.os or "?", report.cpu or "?"),
        _exception_line(report.exception),
        "signature: %s (basis %s) symbolication=%s egress_isolation=%s" % (
            report.signature, report.signature_basis, report.symbolication, report.egress_isolation),
    ]
    for hint in report.hints:
        lines.append("hint: %s [%s] %s" % (hint.kind, hint.confidence, hint.evidence))
    for thread in report.threads:
        title = "crashing thread" if thread.crashed else "thread"
        lines.append("%s %s%s:" % (title, thread.thread_id, (" \"%s\"" % thread.name) if thread.name else ""))
        shown = thread.frames if thread.crashed else thread.frames[:3]
        for frame in shown:
            lines.append("  #%d %s%s [%s]" % (frame.index, frame_label(frame), " (inline)" if frame.inline else "",
                                            frame.trust))
        if thread.frames_truncated or len(shown) < len(thread.frames):
            lines.append("  ...")
    if report.threads_total > len(report.threads):
        lines.append("(%d threads total; %d shown)" % (report.threads_total, len(report.threads)))
    for module in report.modules[:12]:
        lines.append("module %s base=0x%x size=0x%x version=%s id=%s symbols=%s%s%s" % (
            module.name, module.base, module.size, module.version or "-", module.debug_id or "-",
            module.symbols, " project" if module.in_project else "",
            " managed-runtime" if module.managed_runtime else ""))
    for item in report.annotations:
        lines.append("annotation %s=%s" % (item.key, item.value))
    for note in report.notes:
        lines.append("note: %s" % note)
    if report.truncated:
        lines.append("(report truncated)")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 15] + "\n...(clipped)"


def bucket_to_wire(bucket: CrashBucket) -> dict:
    data = _plain(bucket)
    data["sample_labels"] = list(bucket.sample_labels)
    return data


def render_bucket_table(buckets: Sequence[CrashBucket], max_chars: int = 8_000) -> str:
    lines = ["count  signature         basis           exception                      top frame (untrusted)"]
    for bucket in buckets:
        lines.append("%5d  %-16s  %-14s  %-29s  %s" % (bucket.count, bucket.signature, bucket.basis,
                                                     bucket.exception_name[:29], bucket.top_frame))
        if bucket.sample_labels:
            lines.append("       e.g. %s" % ", ".join(bucket.sample_labels))
    text = "\n".join(lines)
    return text[:max_chars]


# --------------------------------------------------------------------- merge

def _stem(name: str) -> str:
    base = str(name or "").lower().replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".exe", ".dll", ".sys", ".so", ".dylib"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


class _Modules:
    def __init__(self, modules: Sequence[ModuleInfo]) -> None:
        self.index = ModuleIndex(modules)
        self.by_stem: dict[str, ModuleInfo] = {}
        for module in modules:
            self.by_stem.setdefault(_stem(module.name), module)

    def lookup(self, name: str) -> ModuleInfo | None:
        return self.by_stem.get(_stem(name)) if name else None


def _enrich(frame: StackFrame, modules: _Modules, index: int) -> StackFrame:
    module = modules.lookup(frame.module)
    offset = frame.module_offset
    if frame.address is not None:
        found = modules.index.find(frame.address)
        if found is not None and (module is None or module is found):
            module = found
            offset = frame.address - found.base
    if module is not None and offset is None and frame.address is not None and module.contains(frame.address):
        offset = frame.address - module.base
    if module is not None:
        in_project = module.in_project
        name = module.name
    else:
        in_project = bool(frame.file) and not is_system_file(frame.file)
        name = frame.module
    return replace(frame, index=index, module=name, module_offset=offset, in_project=in_project)


def _symbolize(frames: Sequence[StackFrame], findings: DebuggerFindings, modules: _Modules) -> tuple[StackFrame, ...]:
    table = {(_stem(item.module), item.offset): item.frames for item in findings.symbolized}
    out: list[StackFrame] = []
    for frame in frames:
        hit = table.get((_stem(frame.module), frame.module_offset)) if frame.module_offset is not None else None
        if hit and any(item.function for item in hit):
            for item in hit:
                out.append(replace(item, index=len(out), address=frame.address, module=frame.module,
                                   module_offset=frame.module_offset, in_project=frame.in_project))
        else:
            out.append(replace(frame, index=len(out)))
    return tuple(out[:MAX_CRASHING_FRAMES])


def merge_findings(base: CrashReport, findings: DebuggerFindings, engine: str) -> CrashReport:
    """Tier-1 findings over a Tier-0 report; the result is re-finalized."""
    engine = str(engine)
    engines = base.engines if engine in base.engines else base.engines + (engine,)
    modules = _Modules(base.modules)
    notes = list(base.notes)
    threads = list(base.threads)
    names = dict(findings.thread_names)
    if findings.symbolized:
        threads = [replace(thread, frames=_symbolize(thread.frames, findings, modules)) for thread in threads]
    elif findings.frames_by_thread:
        by_tid = {tid: frames for tid, frames in findings.frames_by_thread}
        crashing_index = next((i for i, t in enumerate(threads) if t.crashed), None)
        debugger_crash = findings.frames_for(findings.crashing_thread) if findings.crashing_thread is not None \
            else findings.frames_by_thread[0][1]
        if debugger_crash:
            enriched = tuple(_enrich(frame, modules, i) for i, frame in enumerate(debugger_crash))
            if crashing_index is None:
                tid = findings.crashing_thread if findings.crashing_thread is not None else 0
                threads.insert(0, ThreadSummary(thread_id=tid, name=names.get(tid, ""), crashed=True,
                                                frames=enriched))
            else:
                current = threads[crashing_index]
                threads[crashing_index] = replace(current, frames=enriched,
                                                  name=current.name or names.get(findings.crashing_thread, ""))
        real_tids = engine in ("gdb", "cdb", "eu_stack", "minidump_stackwalk")
        known = {thread.thread_id for thread in threads}
        for i, thread in enumerate(threads):
            if thread.crashed or thread.thread_id not in by_tid:
                continue
            frames = by_tid[thread.thread_id]
            threads[i] = replace(thread, frames=tuple(_enrich(f, modules, n) for n, f in enumerate(frames)),
                                 name=thread.name or names.get(thread.thread_id, ""))
        if real_tids:
            for tid, frames in findings.frames_by_thread:
                if tid in known or tid == findings.crashing_thread or len(threads) > MAX_OTHER_THREADS:
                    continue
                threads.append(ThreadSummary(thread_id=tid, name=names.get(tid, ""), crashed=False,
                                             frames=tuple(_enrich(f, modules, n) for n, f in enumerate(frames))))
    else:
        notes.append("%s produced no frames (sections: %s)" % (
            engine, ",".join(findings.sections_seen) or "none"))
    merged_modules = list(base.modules)
    status = {}
    for module in findings.modules:
        if module.symbols != "not_attempted":
            status[_stem(module.name)] = module.symbols
    merged_modules = [replace(module, symbols=status.get(_stem(module.name), module.symbols))
                      for module in merged_modules]
    if not merged_modules and findings.modules:
        merged_modules = list(findings.modules)[:128]
    exception = base.exception
    if exception is None and findings.exception is not None and findings.exception.name:
        exception = findings.exception
    elif exception is not None and findings.exception is not None:
        if exception.access_address is None and findings.exception.access_address is not None:
            exception = replace(exception, access_address=findings.exception.access_address)
        if not exception.access and findings.exception.access:
            exception = replace(exception, access=findings.exception.access)
    for note in findings.notes[:8]:
        notes.append("%s: %s" % (engine, note))
    crashing_id = base.crashing_thread_id
    if crashing_id is None and findings.crashing_thread is not None and exception is not None:
        crashing_id = findings.crashing_thread
    capped = cap_threads(threads, crashing_id, max_crashing_frames=MAX_CRASHING_FRAMES,
                         max_others=MAX_OTHER_THREADS, max_other_frames=MAX_OTHER_FRAMES)
    merged = replace(base, engines=engines, threads=capped, modules=tuple(merged_modules),
                     exception=exception, crashing_thread_id=crashing_id, notes=tuple(notes),
                     modules_total=max(base.modules_total, len(merged_modules)),
                     threads_total=max(base.threads_total, len(threads)))
    return finalize_report(merged)


__all__ = [
    "DEFAULT_WIRE_BYTES", "bucket_to_wire", "frame_to_wire", "merge_findings", "render_bucket_table",
    "render_report", "report_from_wire", "report_to_wire", "wire_size",
]
