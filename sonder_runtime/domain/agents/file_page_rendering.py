"""Pure formatting for the bounded agent-lane file page response."""

from __future__ import annotations

from collections.abc import Mapping


_DEFAULT_OFFSET = 1
_DEFAULT_LIMIT = 120
_MAX_LIMIT = 400
_MAX_LINE_CHARS = 2_000
_MAX_OUTPUT_CHARS = 6_000


def _integer(value: object, *, name: str, default: int) -> int:
    if value is None:
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < 1:
        raise ValueError(f"{name} must be at least 1")
    return result


def normalize_file_page_args(args: Mapping[str, object] | None) -> dict[str, object]:
    """Normalize agent-lane aliases into the guarded line-range contract."""

    supplied = args or {}
    path = next(
        (supplied[key] for key in ("path", "file", "filename", "file_path") if supplied.get(key)),
        None,
    )
    if path is None:
        raise ValueError("path is required")
    start = _integer(
        supplied.get("offset", supplied.get("start_line")),
        name="offset",
        default=_DEFAULT_OFFSET,
    )
    if supplied.get("limit") is None and supplied.get("end_line") is not None:
        end = _integer(supplied["end_line"], name="end_line", default=start)
    else:
        limit = _integer(supplied.get("limit"), name="limit", default=_DEFAULT_LIMIT)
        limit = min(limit, _MAX_LIMIT)
        end = start + limit - 1
    if end < start:
        raise ValueError("end_line must not be before offset")
    return {"path": str(path), "start_line": start, "end_line": min(end, start + _MAX_LIMIT - 1)}


def _display_path(value: object) -> str:
    return str(value or "?").replace("\\", "/")


def _header(path: str, start: int, end: int, total: int) -> str:
    suffix = "(end of file)" if end >= total else f"(next: offset={end + 1})"
    return f"file {path}: lines {start}-{end} of {total} {suffix}"


def _line_text(item: object) -> tuple[int | None, str]:
    if isinstance(item, Mapping):
        number = item.get("line")
        text = item.get("text", "")
        return (int(number) if number is not None else None), str(text).rstrip("\r\n")
    return None, str(item).rstrip("\r\n")


def _bounded_text(item: object) -> str:
    number, text = _line_text(item)
    del number
    full_length = len(text)
    if isinstance(item, Mapping) and item.get("characters") is not None:
        try:
            full_length = max(full_length, int(item["characters"]))
        except (TypeError, ValueError):
            pass
    if full_length <= _MAX_LINE_CHARS:
        return text
    return text[:_MAX_LINE_CHARS] + f"...[+{full_length - _MAX_LINE_CHARS}]"


def render_file_page(data: Mapping[str, object], *, path: str | None = None) -> str:
    """Render an adapter-produced page without reading files or importing adapters."""

    display = _display_path(path if path is not None else data.get("path"))
    if bool(data.get("binary")):
        size = data.get("bytes", data.get("size", 0))
        return f"file {display} is binary; refusing to display ({size} bytes)"

    total = int(data.get("total_lines", data.get("line_count", 0)) or 0)
    if total == 0:
        return f"file {display} is empty (0 lines)"
    start = int(data.get("start_line", data.get("offset", 1)) or 1)
    if start > total:
        return f"file has {total} lines; offset {start} is past the end"

    raw_lines = data.get("lines", ()) or ()
    rendered_lines: list[tuple[int, str]] = []
    for index, item in enumerate(raw_lines):
        number, _ = _line_text(item)
        rendered_lines.append((number if number is not None else start + index, _bounded_text(item)))
    if not rendered_lines:
        return f"file has {total} lines; offset {start} is past the end"

    # Keep complete numbered lines where possible.  The page header is rebuilt
    # as lines are admitted so its next offset remains truthful under the cap.
    chosen: list[tuple[int, str]] = []
    for number, text in rendered_lines:
        candidate = chosen + [(number, text)]
        last = candidate[-1][0]
        body = "\n".join(f"{line:6d}  {value}" for line, value in candidate)
        if len(_header(display, start, last, total) + "\n" + body) > _MAX_OUTPUT_CHARS:
            break
        chosen = candidate
    if not chosen:
        number, text = rendered_lines[0]
        prefix = f"{number:6d}  "
        room = max(0, _MAX_OUTPUT_CHARS - len(_header(display, start, number, total)) - 1 - len(prefix))
        chosen = [(number, text[:room])]

    last = chosen[-1][0]
    header = _header(display, start, last, total)
    body = "\n".join(f"{number:6d}  {text}" for number, text in chosen)
    return (header + "\n" + body)[:_MAX_OUTPUT_CHARS]


__all__ = ["normalize_file_page_args", "render_file_page"]
