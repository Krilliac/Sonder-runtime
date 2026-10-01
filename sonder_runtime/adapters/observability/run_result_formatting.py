"""Verdict-first, model-sized rendering for bounded command and tool runs."""
from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from ..filesystem import file_ops
from ...domain.diagnostics.model import strip_ansi
from ...domain.diagnostics.summary import find_summary


MAX_RESULT_CHARS = 6000
DIGEST_MAX_CHARS = 1200
_FILE_LINE = re.compile(r"(?<!\S)(?:[A-Za-z]:)?[^\s:]+:[1-9]\d*\b")


def _clip(text: object, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:max(0, limit - 3)] + "..."


def _metadata(lines: list[str], budget: int) -> str:
    """Shorten long values only when the complete metadata will not fit."""
    low, high = 0, budget
    while low < high:
        cap = (low + high + 1) // 2
        if len("\n".join(_clip(line, cap) for line in lines)) <= budget:
            low = cap
        else:
            high = cap - 1
    return "\n".join(_clip(line, low) for line in lines)


def _digest(text: str, limit: int, streams: list[tuple[str, str]]) -> str:
    cleaned = strip_ansi(text)
    lines = cleaned.splitlines()
    # Noisy stderr must not hide the pytest verdict at the end of stdout.
    summaries = []
    for _, value in streams:
        stream_lines = strip_ansi(value).splitlines()
        summary = find_summary(stream_lines)
        if summary is not None:
            summaries.append(summary.line)
        else:
            short = next((line.strip() for line in reversed(stream_lines)
                          if re.fullmatch(r"\d+ (?:failed|passed|errors?)", line.strip())), None)
            if short:
                summaries.append(short)
    last = next((line.strip() for line in reversed(lines) if line.strip()), "(no output)")
    result = ["digest:"] + ["  summary: " + _clip(line, 400)
                            for line in dict.fromkeys(summaries or [last])]
    failures = list(dict.fromkeys(
        line.strip() for line in lines if re.match(r"^\s*(?:FAILED|ERROR)\b", line)
    ))[:20]
    locations = list(dict.fromkeys(match.group() for match in _FILE_LINE.finditer(cleaned)))[:10]
    # A long failure list must not crowd out the first source locations.
    room = max(0, limit - len("\n".join(result)) - 2)
    failure_budget = room * 2 // 3 if locations else room
    location_budget = room - failure_budget
    for label, values, budget in (
        ("  failure lines:", failures, failure_budget),
        ("  first locations:", locations, location_budget),
    ):
        if not values or budget <= len(label) + 10:
            continue
        section = [label]
        for value in values:
            available = budget - len("\n".join(section)) - 5
            if available < 8:
                break
            section.append("    " + _clip(value, min(230, available)))
        result.extend(section)
    return _clip("\n".join(result), limit)


def _output_reference(data: dict, text: str) -> str:
    """Persist only complete captures, using the existing mutation path guard.

    A bounded capture cannot be reconstructed into a full log. Producers that
    stream to disk can supply output_log/output_bytes instead.
    """
    path = data.get("output_log")
    if path:
        try:
            guarded = file_ops.resolve_mutation_path(str(path))
            root = file_ops.resolve_mutation_path(str(Path(data["cwd"]) / ".sonder" / "run"))
            if guarded.parent != root or guarded.suffix != ".log":
                raise ValueError("log is not inside workspace/.sonder/run")
            size = guarded.stat().st_size
        except (KeyError, OSError, ValueError) as exc:
            return "full output: unavailable (%s)" % _clip(exc, 180)
    if not path:
        if (data.get("stdout_truncated") or data.get("stderr_truncated")
                or "language" in data):
            return "full output: unavailable (producer retained only an output window)"
        if not data.get("cwd"):
            return "full output: unavailable (no workspace path)"
        try:
            root = Path(data["cwd"]) / ".sonder" / "run"
            target = root / (uuid4().hex + ".log")
            guarded = file_ops.resolve_mutation_path(str(target))
            guarded.parent.mkdir(parents=True, exist_ok=True)
            # Recheck after creating parents; never follow a .sonder junction.
            guarded = file_ops.resolve_mutation_path(str(target))
            payload = text.encode("utf-8", errors="replace")
            with guarded.open("xb") as handle:
                handle.write(payload)
            path, size = str(guarded), len(payload)
        except (OSError, ValueError) as exc:
            return "full output: unavailable (%s)" % _clip(exc, 180)
    return "full output: %s (%s bytes); page it with file_read offset=" % (path, size)


def _code_notes(data: dict) -> list[str]:
    """Preserve run_code's non-interactive recovery advice."""
    if "language" not in data:
        return []
    notes = []
    stdout, stderr = data.get("stdout") or "", data.get("stderr") or ""
    if "EOFError" in stderr:
        notes.append("note: this program tried to read keyboard input, but /run is non-interactive.")
    if (data.get("error") or "").startswith("timed out"):
        if data.get("language") in ("csharp", "cpp", "project") and any(
            word in stdout.lower() for word in ("enter", "guess", "input")
        ):
            notes.append("note: the program appears to be waiting for console input. For /run, add a scripted demo path or provide stdin.")
        notes.append("note: use a bounded smoke test or auto-exit path for /run.")
    return notes


def format_run_result(title: str, data: dict, *, digest: bool = False, context: str = "") -> str:
    """Keep the verdict and digest before streams, within the 6,000-char view.

    Small streams are lossless; larger ones retain a 1,500-character head and
    2,500-character tail. Metadata and digest have separate budgets so neither
    can displace the tail. ``digest`` remains opt-in for existing callers.

    ``context`` is what the caller established before the run (``script_run``'s
    artifact-risk report). It directly follows the exit line, ahead of every
    field the run produced, the digest and the streams, and is never clipped:
    its length comes out of the output window instead, head first.
    """
    timed_out = data.get("timed_out", False) or (data.get("error") or "").startswith("timed out")
    status = "timed_out" if timed_out else "ok" if data.get("ok") else "failed"
    if not timed_out and data.get("returncode") is None and data.get("error"):
        status = "error"
    elapsed = float(data.get("elapsed_ms") or 0) / 1000
    verdict = "exit %s (%s, %.3f s)" % (data.get("returncode"), status, elapsed)
    metadata = [
        title,
        "  command: %s" % json.dumps(data.get("command") or [], ensure_ascii=False),
        "  cwd: %s" % data.get("cwd", ""),
        "  ok: %s" % data.get("ok", False),
        "  returncode: %s" % data.get("returncode"),
        "  timed_out: %s" % data.get("timed_out", False),
        "  elapsed_ms: %s" % data.get("elapsed_ms", 0),
    ]
    # Infrastructure classifiers read these before the stdout marker.
    for field in ("error", "guard", "holder", "recovery", "language", "timeout"):
        if data.get(field):
            value = data[field]
            if field == "guard" and data.get("guard_reason"):
                value = "%s (%s)" % (value, data["guard_reason"])
            metadata.append("  %s: %s" % (field, value))
    metadata.extend(_code_notes(data))
    # The context is paid for by the output window (head, then tail), so the
    # metadata and digest budgets below are the same with or without it.
    reserve = len(context) + 1 if context else 0
    head, tail = max(0, 1500 - reserve), max(0, 2500 - max(0, reserve - 1500))
    stdout, stderr = data.get("stdout") or "", data.get("stderr") or ""
    streams = [(name, value) for name, value in (("stdout", stdout), ("stderr", stderr)) if value]
    combined = "\n".join(value for _, value in streams)
    full = len(combined) <= max(0, 5000 - reserve)
    if full:
        output = "\n".join(name + ":\n" + value for name, value in streams)
    else:
        # Slice content separately from labels to retain exactly the promised
        # number of output characters, including a tail spanning both streams.
        def window(start: int, end: int) -> str:
            parts, offset = [], 0
            for name, value in streams:
                left, right = max(0, start - offset), min(len(value), end - offset)
                if left < right:
                    parts.append(name + ":\n" + value[left:right])
                offset += len(value) + 1
            return "\n".join(parts)

        output = "\n".join(part for part in (
            window(0, head), "... (output omitted) ...", window(len(combined) - tail, len(combined)),
        ) if part)
    footer = []
    if not full:
        footer.append(_output_reference(data, combined))
    if data.get("stdout_truncated") or data.get("stderr_truncated"):
        footer.append("  output truncated: true")
    footer_text = _clip("\n".join(footer), 500)
    # Retain every metadata field name, even with pathological argv/errors.
    metadata_text = _metadata(
        metadata, min(1500, MAX_RESULT_CHARS - reserve - len(output) - len(footer_text) - 500),
    )
    available = MAX_RESULT_CHARS - reserve - sum(map(len, (verdict, metadata_text, output, footer_text))) - 5
    block = _digest(combined, min(DIGEST_MAX_CHARS, available), streams) if digest and combined else ""
    return "\n".join(part for part in (verdict, context, metadata_text, block, output, footer_text) if part)
