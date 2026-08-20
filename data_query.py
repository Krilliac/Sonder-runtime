"""Bounded, read-only queries over guarded SQLite and structured text files."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import re
import sqlite3
import time

import sonder_runtime.adapters.filesystem.file_ops as file_ops


DEFAULT_MAX_ROWS = 100
MAX_ROWS = 1000
DEFAULT_MAX_COLUMNS = 50
MAX_COLUMNS = 100
DEFAULT_OUTPUT_BYTES = 256_000
MAX_OUTPUT_BYTES = 512_000
DEFAULT_SCAN_BYTES = 4_000_000
MAX_SCAN_BYTES = 64_000_000
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 15.0
MAX_SQL_BYTES = 65_536
MAX_SELECTORS = 100
MAX_SELECTOR_CHARS = 1024
MAX_CELL_BYTES = 256_000
_MISSING = object()
_SQL_START_RE = re.compile(r"(?:SELECT|WITH)\b", re.IGNORECASE)

SQLITE_FUNCTIONS = frozenset({
    "abs", "avg", "char", "coalesce", "count", "date", "datetime",
    "glob", "hex", "ifnull", "instr", "julianday", "length", "like",
    "likelihood", "likely", "lower", "ltrim", "max", "min", "nullif",
    "printf", "quote", "replace", "round", "rtrim",
    "strftime", "substr", "substring", "sum", "time", "total", "trim",
    "typeof", "unicode", "unixepoch", "unlikely", "upper", "zeroblob",
})


class DataQueryError(RuntimeError):
    """A stable rejection from the bounded data query surface."""


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _bounded_timeout(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(0.05, min(value, MAX_TIMEOUT_SECONDS))


def _resolve_target(
    path, *, extra_roots="", bypass=False, developer_authorized=False,
) -> Path:
    requested = file_ops._requested_path(str(path or ""))
    if file_ops._is_reparse_point(requested):
        raise DataQueryError("query target must not be a symlink or junction")
    try:
        target = file_ops.require_read_access(
            str(path or ""), extra_roots=extra_roots, bypass=bypass,
            developer_authorized=developer_authorized,
        )
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        raise DataQueryError("query path rejected: %s" % exc) from exc
    if not target.exists() or not target.is_file():
        raise DataQueryError("query target must be an existing regular file")
    for root in file_ops.allowed_roots(extra_roots if bypass else ""):
        root = file_ops._resolve_best_effort(root)
        if target == root or file_ops._is_inside(target, root):
            relative = target.relative_to(root)
            if any(
                part.lower() in file_ops.SENSITIVE_READ_DIRECTORIES
                for part in relative.parts
            ):
                raise DataQueryError("query path is secret or repository control state")
            break
    return target


def _reject_json_constant(value):
    raise ValueError("non-finite JSON number is not supported: %s" % value)


def parse_projection(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise DataQueryError("projection_json must be valid JSON") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DataQueryError("projection_json must be a JSON list of strings")
    if len(value) > MAX_SELECTORS or len(set(value)) != len(value):
        raise DataQueryError("projection has too many or duplicate selectors")
    if any(not item or len(item) > MAX_SELECTOR_CHARS for item in value):
        raise DataQueryError("projection contains an empty or oversized selector")
    return list(value)


def parse_filters(value) -> dict:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise DataQueryError("filters_json must be valid JSON") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DataQueryError("filters_json must be a JSON object")
    if len(value) > MAX_SELECTORS:
        raise DataQueryError("filters_json has too many selectors")
    if any(not key or len(key) > MAX_SELECTOR_CHARS for key in value):
        raise DataQueryError("filters_json contains an empty or oversized selector")
    return dict(value)


def _pointer(record, selector):
    if not selector.startswith("/"):
        return record.get(selector, _MISSING) if isinstance(record, dict) else _MISSING
    current = record
    for raw in selector[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if "~" in token and re.search(r"~(?![01])", raw):
            raise DataQueryError("JSON pointer contains an invalid escape")
        if isinstance(current, dict):
            current = current.get(token, _MISSING)
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            current = current[index] if index < len(current) else _MISSING
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def _matches(record, filters):
    for selector, expected in filters.items():
        actual = _pointer(record, selector)
        if actual is _MISSING or type(actual) is not type(expected) or actual != expected:
            return False
    return True


def _project(record, projection):
    if not projection:
        return record
    return {
        selector: (None if value is _MISSING else value)
        for selector in projection
        for value in [_pointer(record, selector)]
    }


def _normalize_value(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return str(value)


def _encoded(result):
    return json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _fit_result(result, max_output):
    while True:
        result["output_bytes"] = 0
        actual = 0
        for _ in range(10):
            actual = len(_encoded(result).encode("utf-8"))
            if result["output_bytes"] == actual:
                break
            result["output_bytes"] = actual
        actual = len(_encoded(result).encode("utf-8"))
        if actual <= max_output:
            result["output_bytes"] = actual
            if len(_encoded(result).encode("utf-8")) == actual:
                return result
        if result["rows"]:
            result["rows"].pop()
            result["count"] = len(result["rows"])
            result["truncated"] = True
            continue
        raise DataQueryError("query metadata exceeds the output byte ceiling")


def _append_row_bounded(result, row, max_output):
    """Append only when the complete envelope remains inside its byte cap."""
    result["rows"].append(row)
    result["count"] = len(result["rows"])
    result["output_bytes"] = 0
    if len(_encoded(result).encode("utf-8")) <= max_output:
        return True
    result["rows"].pop()
    result["count"] = len(result["rows"])
    result["truncated"] = True
    return False


def encode_result(result) -> str:
    """Stable compact JSON encoding used by the MCP wrapper and tests."""
    return _encoded(result)


def _base_result(target, kind, max_rows, max_columns, max_output):
    return {
        "ok": True,
        "path": str(target),
        "kind": kind,
        "limits": {
            "rows": max_rows, "columns": max_columns,
            "output_bytes": max_output,
        },
        "columns": [],
        "rows": [],
        "count": 0,
        "truncated": False,
        "output_bytes": 0,
    }


def _leading_sql_keyword(sql):
    position = 0
    length = len(sql)
    while position < length:
        whitespace = re.match(r"\s+", sql[position:])
        if whitespace:
            position += whitespace.end()
            continue
        if sql.startswith("--", position):
            newline = sql.find("\n", position + 2)
            position = length if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", position):
            end = sql.find("*/", position + 2)
            if end < 0:
                raise DataQueryError("SQL contains an unterminated comment")
            position = end + 2
            continue
        break
    match = _SQL_START_RE.match(sql, position)
    return match.group(0).upper() if match else ""


def _sqlite_authorizer():
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
    recursive = getattr(sqlite3, "SQLITE_RECURSIVE", None)
    if recursive is not None:
        allowed.add(recursive)

    def authorize(action, arg1, arg2, database, trigger):
        if action not in allowed:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            function = str(arg2 or arg1 or "").lower()
            if function not in SQLITE_FUNCTIONS:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


def _query_sqlite(target, sql, *, max_rows, max_columns, max_output, timeout):
    if not isinstance(sql, str) or not sql.strip():
        raise DataQueryError("sql is required for SQLite data_query")
    if "\x00" in sql or len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise DataQueryError("SQL exceeds the supported syntax or byte ceiling")
    if _leading_sql_keyword(sql) not in {"SELECT", "WITH"}:
        raise DataQueryError("SQLite data_query accepts exactly one SELECT or CTE statement")
    deadline = time.monotonic() + timeout
    uri = target.as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
    except sqlite3.Error as exc:
        raise DataQueryError("SQLite open failed: %s" % exc) from exc
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA temp_store=MEMORY")
        if hasattr(conn, "setlimit"):
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_CELL_BYTES)
        conn.set_progress_handler(
            lambda: 1 if time.monotonic() >= deadline else 0, 1000,
        )
        conn.set_authorizer(_sqlite_authorizer())
        try:
            cursor = conn.execute(sql)
        except sqlite3.Error as exc:
            raise DataQueryError("SQLite query rejected: %s" % exc) from exc
        if cursor.description is None:
            raise DataQueryError("SQLite query did not produce a result set")
        columns = [str(column[0]) for column in cursor.description]
        if len(columns) > max_columns:
            raise DataQueryError("SQLite query exceeds the column ceiling")
        if len(set(columns)) != len(columns):
            raise DataQueryError("SQLite query returned duplicate column names")
        result = _base_result(target, "sqlite", max_rows, max_columns, max_output)
        result["columns"] = columns
        for row in cursor:
            if len(result["rows"]) >= max_rows:
                result["truncated"] = True
                break
            row_record = {
                columns[index]: _normalize_value(value)
                for index, value in enumerate(row)
            }
            if not _append_row_bounded(result, row_record, max_output):
                break
        return _fit_result(result, max_output)
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
        conn.close()


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise DataQueryError("data query exceeded the timeout ceiling")


def _iter_json(target, max_scan, deadline):
    _check_deadline(deadline)
    if target.stat().st_size > max_scan:
        raise DataQueryError("JSON file exceeds the scan byte ceiling")
    try:
        _check_deadline(deadline)
        with target.open("r", encoding="utf-8") as handle:
            payload = json.load(handle, parse_constant=_reject_json_constant)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        _check_deadline(deadline)
        raise DataQueryError("malformed JSON input: %s" % exc) from exc
    _check_deadline(deadline)
    return iter(payload if isinstance(payload, list) else [payload])


def _iter_jsonl(target, max_scan, deadline):
    _check_deadline(deadline)
    if target.stat().st_size > max_scan:
        raise DataQueryError("JSONL file exceeds the scan byte ceiling")

    def records():
        try:
            with target.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    _check_deadline(deadline)
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line, parse_constant=_reject_json_constant)
                    except (ValueError, RecursionError) as exc:
                        _check_deadline(deadline)
                        raise DataQueryError(
                            "malformed JSONL input at line %d: %s" % (line_number, exc)
                        ) from exc
                    _check_deadline(deadline)
                    yield record
        except UnicodeError as exc:
            raise DataQueryError("malformed UTF-8 JSONL input") from exc

    return records()


def _iter_delimited(target, max_scan, delimiter, deadline):
    _check_deadline(deadline)
    if target.stat().st_size > max_scan:
        raise DataQueryError("delimited file exceeds the scan byte ceiling")

    def records():
        try:
            with target.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle, delimiter=delimiter)
                fields = reader.fieldnames or []
                if not fields or any(not field for field in fields) or len(set(fields)) != len(fields):
                    raise DataQueryError("CSV/TSV header is empty or contains duplicates")
                if len(fields) > MAX_COLUMNS:
                    raise DataQueryError("CSV/TSV input exceeds the column ceiling")
                for row in reader:
                    _check_deadline(deadline)
                    if None in row:
                        raise DataQueryError("CSV/TSV row has more fields than its header")
                    yield dict(row)
        except (csv.Error, UnicodeError) as exc:
            raise DataQueryError("malformed CSV/TSV input: %s" % exc) from exc

    return records()


def _query_structured(
    target, kind, projection, filters, *, max_rows, max_columns,
    max_output, max_scan, timeout,
):
    deadline = time.monotonic() + timeout
    if kind == "json":
        records = _iter_json(target, max_scan, deadline)
    elif kind == "jsonl":
        records = _iter_jsonl(target, max_scan, deadline)
    else:
        records = _iter_delimited(
            target, max_scan, "," if kind == "csv" else "\t", deadline,
        )
    if len(projection) > max_columns:
        raise DataQueryError("projection exceeds the column ceiling")
    result = _base_result(target, kind, max_rows, max_columns, max_output)
    result["columns"] = list(projection)
    observed_columns = set(projection)
    for record in records:
        _check_deadline(deadline)
        if not isinstance(record, dict):
            raise DataQueryError("structured query records must be JSON objects")
        if not _matches(record, filters):
            continue
        if len(result["rows"]) >= max_rows:
            result["truncated"] = True
            break
        if not projection:
            observed_columns.update(str(key) for key in record)
            if len(observed_columns) > max_columns:
                raise DataQueryError("structured query exceeds the column ceiling")
            result["columns"] = sorted(observed_columns)
        if not _append_row_bounded(
            result, _project(record, projection), max_output,
        ):
            break
    return _fit_result(result, max_output)


def query_data(
    path, *, sql="", projection=None, filters=None,
    max_rows=DEFAULT_MAX_ROWS, max_columns=DEFAULT_MAX_COLUMNS,
    max_output_bytes=DEFAULT_OUTPUT_BYTES, max_scan_bytes=DEFAULT_SCAN_BYTES,
    timeout=DEFAULT_TIMEOUT_SECONDS, extra_roots="", bypass=False,
    developer_authorized=False,
):
    """Execute one bounded read-only query and return a JSON-safe result."""
    target = _resolve_target(
        path, extra_roots=extra_roots, bypass=bypass,
        developer_authorized=developer_authorized,
    )
    max_rows = _bounded_int(max_rows, DEFAULT_MAX_ROWS, 1, MAX_ROWS)
    max_columns = _bounded_int(max_columns, DEFAULT_MAX_COLUMNS, 1, MAX_COLUMNS)
    max_output = _bounded_int(
        max_output_bytes, DEFAULT_OUTPUT_BYTES, 1024, MAX_OUTPUT_BYTES,
    )
    max_scan = _bounded_int(
        max_scan_bytes, DEFAULT_SCAN_BYTES, 1024, MAX_SCAN_BYTES,
    )
    timeout = _bounded_timeout(timeout)
    suffix = target.suffix.lower()
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        return _query_sqlite(
            target, sql, max_rows=max_rows, max_columns=max_columns,
            max_output=max_output, timeout=timeout,
        )
    kinds = {
        ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
        ".csv": "csv", ".tsv": "tsv",
    }
    kind = kinds.get(suffix)
    if not kind:
        raise DataQueryError("data_query supports SQLite, JSON, JSONL, CSV, and TSV")
    if str(sql or "").strip():
        raise DataQueryError("sql is only accepted for SQLite files")
    return _query_structured(
        target, kind, parse_projection(projection), parse_filters(filters),
        max_rows=max_rows, max_columns=max_columns, max_output=max_output,
        max_scan=max_scan, timeout=timeout,
    )
