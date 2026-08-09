"""Guarded deterministic conversion among JSON, JSONL, CSV, and TSV."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import secrets
import stat
import time

import file_ops
import symbol_index


FORMATS = frozenset({"json", "jsonl", "csv", "tsv"})
SUFFIX_FORMATS = {
    ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
    ".csv": "csv", ".tsv": "tsv",
}
DEFAULT_MAX_INPUT_BYTES = 16_000_000
HARD_MAX_INPUT_BYTES = 64_000_000
DEFAULT_MAX_OUTPUT_BYTES = 16_000_000
HARD_MAX_OUTPUT_BYTES = 64_000_000
DEFAULT_MAX_ROWS = 10_000
HARD_MAX_ROWS = 100_000
DEFAULT_MAX_COLUMNS = 100
HARD_MAX_COLUMNS = 500
DEFAULT_MAX_FIELDS = 50
HARD_MAX_FIELDS = 100
DEFAULT_MAX_FIELD_BYTES = 64_000
HARD_MAX_FIELD_BYTES = 256_000
DEFAULT_MAX_DEPTH = 16
HARD_MAX_DEPTH = 64
DEFAULT_PREVIEW_ROWS = 5
HARD_MAX_PREVIEW_ROWS = 20
DEFAULT_TIMEOUT_SECONDS = 10.0
HARD_MAX_TIMEOUT_SECONDS = 30.0
HARD_MAX_FIELD_NAME_CHARS = 256
HARD_MAX_REPORT_BYTES = 256_000


class DataConvertError(RuntimeError):
    """A stable rejection from the bounded conversion surface."""


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
    return max(0.05, min(value, HARD_MAX_TIMEOUT_SECONDS))


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise DataConvertError("data conversion exceeded the timeout ceiling")


def _reject_constant(value):
    raise ValueError("non-finite JSON number is not supported: %s" % value)


def parse_fields(value, *, max_fields=DEFAULT_MAX_FIELDS):
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_constant=_reject_constant)
        except (TypeError, ValueError) as exc:
            raise DataConvertError("fields_json must be valid JSON") from exc
    if not isinstance(value, list) or not value:
        raise DataConvertError("fields_json must be a non-empty JSON list")
    if any(not isinstance(item, str) for item in value):
        raise DataConvertError("fields_json must contain only strings")
    if len(value) > max_fields:
        raise DataConvertError("field selection exceeds the field ceiling")
    if len(set(value)) != len(value):
        raise DataConvertError("field selection contains duplicates")
    if any(
        not item or "\x00" in item or len(item) > HARD_MAX_FIELD_NAME_CHARS
        for item in value
    ):
        raise DataConvertError("field selection contains an empty or oversized name")
    return list(value)


def _requested_path(raw, label):
    text = str(raw or "").strip()
    if not text or "\x00" in text or file_ops._foreign_absolute(text):
        raise DataConvertError("%s must be a non-empty native path" % label)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = file_ops.workspace_root() / candidate
    return candidate.absolute()


def _reject_reparse_components(path, label):
    current = Path(os.path.normpath(str(path)))
    while True:
        if file_ops._is_reparse_point(current):
            raise DataConvertError("%s must not traverse a symlink or junction" % label)
        parent = current.parent
        if parent == current:
            return
        current = parent


def _authorized_root(target, extra_roots):
    roots = [file_ops._resolve_best_effort(root) for root in file_ops.allowed_roots(extra_roots)]
    return next((
        root for root in roots
        if target == root or file_ops._is_inside(target, root)
    ), None)


def _resolve_input(path, extra_roots):
    requested = _requested_path(path, "input path")
    _reject_reparse_components(requested, "input path")
    try:
        target = file_ops.resolve_repository_read_path(
            str(path), allow_workspace_root=False, reject_sensitive=True,
            extra_roots=extra_roots,
        )
        metadata = target.lstat()
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        raise DataConvertError("input path rejected: %s" % exc) from exc
    if file_ops._is_reparse_point(target) or not stat.S_ISREG(metadata.st_mode):
        raise DataConvertError("input path must be a regular non-symlink file")
    return target


def _resolve_output(path, extra_roots):
    requested = _requested_path(path, "output path")
    _reject_reparse_components(requested, "output path")
    target = Path(os.path.normpath(str(requested)))
    root = _authorized_root(target, extra_roots)
    if root is None:
        raise DataConvertError("output path is outside every authorized root")
    relative = target.relative_to(root)
    if (
        file_ops._is_protected_mutation_path(target)
        or any(part.lower() in file_ops.SENSITIVE_READ_DIRECTORIES for part in relative.parts)
    ):
        raise DataConvertError("output path is secret or control state")
    if target.exists() or target.is_symlink():
        raise DataConvertError("output path already exists; conversion never overwrites")
    if not target.parent.exists() or not target.parent.is_dir():
        raise DataConvertError("output parent must be an existing directory")
    if file_ops._is_reparse_point(target.parent):
        raise DataConvertError("output parent must not be a symlink or junction")
    return target, root


def _format_for_input(path):
    try:
        return SUFFIX_FORMATS[path.suffix.lower()]
    except KeyError as exc:
        raise DataConvertError("input must use .json, .jsonl/.ndjson, .csv, or .tsv") from exc


def _format_for_output(path, requested):
    requested = str(requested or "").strip().lower()
    suffix_format = SUFFIX_FORMATS.get(path.suffix.lower(), "")
    output_format = requested or suffix_format
    if output_format not in FORMATS:
        raise DataConvertError("output_format must be json, jsonl, csv, or tsv")
    if suffix_format and suffix_format != output_format:
        raise DataConvertError("output_format conflicts with the output filename suffix")
    if not suffix_format:
        raise DataConvertError("output filename must use .json, .jsonl, .csv, or .tsv")
    return output_format


def _validate_value(value, *, depth, max_depth, max_columns):
    if depth > max_depth:
        raise DataConvertError("input value exceeds the nesting depth ceiling")
    if isinstance(value, str):
        if "\x00" in value:
            raise DataConvertError("input contains a NUL character")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DataConvertError("input contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_value(
                item, depth=depth + 1, max_depth=max_depth,
                max_columns=max_columns,
            )
        return
    if isinstance(value, dict):
        if len(value) > max_columns:
            raise DataConvertError("input object exceeds the column ceiling")
        for key, item in value.items():
            if (
                not isinstance(key, str) or not key or "\x00" in key
                or len(key) > HARD_MAX_FIELD_NAME_CHARS
            ):
                raise DataConvertError("input object keys must be bounded non-empty strings")
            _validate_value(
                item, depth=depth + 1, max_depth=max_depth,
                max_columns=max_columns,
            )
        return
    raise DataConvertError("input contains an unsupported value type")


def _field_bytes(value):
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8"))


def _canonical_value(value):
    if isinstance(value, dict):
        return {
            key: _canonical_value(value[key])
            for key in sorted(value)
        }
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return value


def _selected_record(record, fields, limits):
    if not isinstance(record, dict):
        raise DataConvertError("every input row must be an object")
    if len(record) > limits["max_columns"]:
        raise DataConvertError("input row exceeds the column ceiling")
    _validate_value(
        record, depth=1, max_depth=limits["max_depth"],
        max_columns=limits["max_columns"],
    )
    for value in record.values():
        if _field_bytes(value) > limits["max_field_bytes"]:
            raise DataConvertError("input value exceeds the field byte ceiling")
    selected = {
        field: _canonical_value(record.get(field))
        for field in fields
    }
    return selected


def _input_records(text, input_format, limits, deadline):
    if input_format == "json":
        try:
            payload = json.loads(text, parse_constant=_reject_constant)
        except (TypeError, ValueError, RecursionError) as exc:
            _check_deadline(deadline)
            raise DataConvertError("malformed JSON input: %s" % exc) from exc
        _check_deadline(deadline)
        if not isinstance(payload, list):
            raise DataConvertError("JSON input must be an array of objects")
        yield from payload
        return
    stream = io.StringIO(text, newline="")
    if input_format == "jsonl":
        for line_number, line in enumerate(stream, 1):
            _check_deadline(deadline)
            if not line.strip():
                continue
            try:
                yield json.loads(line, parse_constant=_reject_constant)
            except (TypeError, ValueError, RecursionError) as exc:
                raise DataConvertError(
                    "malformed JSONL input at line %d: %s" % (line_number, exc)
                ) from exc
        return
    reader = csv.DictReader(stream, delimiter="," if input_format == "csv" else "\t")
    headers = reader.fieldnames or []
    if not headers or any(not header for header in headers):
        raise DataConvertError("delimited input has an empty header")
    if len(headers) != len(set(headers)):
        raise DataConvertError("delimited input has duplicate headers")
    if any("\x00" in header or len(header) > HARD_MAX_FIELD_NAME_CHARS for header in headers):
        raise DataConvertError("delimited input has an invalid or oversized header")
    if len(headers) > limits["max_columns"]:
        raise DataConvertError("delimited input exceeds the column ceiling")
    try:
        for row_number, row in enumerate(reader, 2):
            _check_deadline(deadline)
            if None in row or any(value is None for value in row.values()):
                raise DataConvertError(
                    "delimited input row %d does not match its header" % row_number
                )
            yield dict(row)
    except csv.Error as exc:
        raise DataConvertError("malformed delimited input: %s" % exc) from exc


def _delimited_value(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    return str(value)


class _OutputSink:
    def __init__(self, binary, max_bytes):
        self.binary = binary
        self.max_bytes = max_bytes
        self.bytes = 0
        self.digest = hashlib.sha256()

    def write(self, text):
        encoded = str(text).encode("utf-8")
        if self.bytes + len(encoded) > self.max_bytes:
            raise DataConvertError("converted output exceeds the output byte ceiling")
        if self.binary is not None:
            self.binary.write(encoded)
        self.digest.update(encoded)
        self.bytes += len(encoded)
        return len(str(text))


def _convert_records(records, fields, output_format, sink, limits, deadline):
    preview_rows = []
    row_count = 0
    writer = None
    if output_format == "json":
        sink.write("[")
    elif output_format in {"csv", "tsv"}:
        writer = csv.writer(
            sink, delimiter="," if output_format == "csv" else "\t",
            lineterminator="\n", quoting=csv.QUOTE_MINIMAL,
        )
        writer.writerow(fields)
    for record in records:
        _check_deadline(deadline)
        if row_count >= limits["max_rows"]:
            raise DataConvertError("input exceeds the row ceiling")
        selected = _selected_record(record, fields, limits)
        if len(preview_rows) < limits["preview_rows"]:
            preview_rows.append(selected)
        if output_format == "json":
            if row_count:
                sink.write(",")
            sink.write(json.dumps(
                selected, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False,
            ))
        elif output_format == "jsonl":
            sink.write(json.dumps(
                selected, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False,
            ) + "\n")
        else:
            writer.writerow([_delimited_value(selected[field]) for field in fields])
        row_count += 1
    if output_format == "json":
        sink.write("]\n")
    _check_deadline(deadline)
    return row_count, preview_rows


def _temporary_path(output):
    return output.parent / (".%s.sonder-convert-%s.tmp" % (
        output.name, secrets.token_hex(8),
    ))


def _publish_non_overwrite(temporary, output, root):
    _reject_reparse_components(output, "output path")
    if output.exists() or output.is_symlink():
        raise DataConvertError("output path appeared before publication")
    current_parent = file_ops._resolve_best_effort(output.parent)
    expected_parent = Path(os.path.normpath(str(output.parent)))
    if (
        os.path.normcase(str(current_parent)) != os.path.normcase(str(expected_parent))
        or not file_ops._is_inside(output, root)
    ):
        raise DataConvertError("output containment changed before publication")
    try:
        os.link(temporary, output)
    except FileExistsError as exc:
        raise DataConvertError("output path appeared before publication") from exc
    except OSError as exc:
        raise DataConvertError("atomic non-overwrite publication failed: %s" % exc) from exc
    published = output.lstat()
    staged = temporary.lstat()
    if (
        file_ops._is_reparse_point(output)
        or not stat.S_ISREG(published.st_mode)
        or not os.path.samestat(staged, published)
    ):
        raise DataConvertError("published output identity could not be verified")
    temporary.unlink()


def _encoded_report(report):
    return json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def encode_result(report):
    return _encoded_report(report)


def _fit_report(report):
    report["report_bytes"] = 0
    while True:
        for _ in range(10):
            actual = len(_encoded_report(report).encode("utf-8"))
            if actual == report["report_bytes"]:
                break
            report["report_bytes"] = actual
        actual = len(_encoded_report(report).encode("utf-8"))
        if actual <= HARD_MAX_REPORT_BYTES and actual == report["report_bytes"]:
            return report
        if report["preview_rows"]:
            report["preview_rows"].pop()
            report["preview_truncated"] = True
            continue
        raise DataConvertError("conversion report exceeds the report byte ceiling")


def convert_data(
    input_path, output_path, fields, *, output_format="", apply=False,
    max_input_bytes=DEFAULT_MAX_INPUT_BYTES,
    max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES, max_rows=DEFAULT_MAX_ROWS,
    max_columns=DEFAULT_MAX_COLUMNS, max_fields=DEFAULT_MAX_FIELDS,
    max_field_bytes=DEFAULT_MAX_FIELD_BYTES, max_depth=DEFAULT_MAX_DEPTH,
    preview_rows=DEFAULT_PREVIEW_ROWS, timeout=DEFAULT_TIMEOUT_SECONDS,
    extra_roots="",
):
    """Validate and preview or atomically publish one deterministic conversion."""
    limits = {
        "max_input_bytes": _bounded_int(
            max_input_bytes, DEFAULT_MAX_INPUT_BYTES, 1, HARD_MAX_INPUT_BYTES,
        ),
        "max_output_bytes": _bounded_int(
            max_output_bytes, DEFAULT_MAX_OUTPUT_BYTES, 1, HARD_MAX_OUTPUT_BYTES,
        ),
        "max_rows": _bounded_int(max_rows, DEFAULT_MAX_ROWS, 1, HARD_MAX_ROWS),
        "max_columns": _bounded_int(
            max_columns, DEFAULT_MAX_COLUMNS, 1, HARD_MAX_COLUMNS,
        ),
        "max_fields": _bounded_int(
            max_fields, DEFAULT_MAX_FIELDS, 1, HARD_MAX_FIELDS,
        ),
        "max_field_bytes": _bounded_int(
            max_field_bytes, DEFAULT_MAX_FIELD_BYTES, 1, HARD_MAX_FIELD_BYTES,
        ),
        "max_depth": _bounded_int(max_depth, DEFAULT_MAX_DEPTH, 1, HARD_MAX_DEPTH),
        "preview_rows": _bounded_int(
            preview_rows, DEFAULT_PREVIEW_ROWS, 0, HARD_MAX_PREVIEW_ROWS,
        ),
        "timeout_seconds": _bounded_timeout(timeout),
    }
    apply = apply is True
    deadline = time.monotonic() + limits["timeout_seconds"]
    selected_fields = parse_fields(fields, max_fields=limits["max_fields"])
    input_target = _resolve_input(input_path, extra_roots)
    output_target, output_root = _resolve_output(output_path, extra_roots)
    if os.path.normcase(str(input_target)) == os.path.normcase(str(output_target)):
        raise DataConvertError("input and output paths must be different")
    input_format = _format_for_input(input_target)
    selected_output_format = _format_for_output(output_target, output_format)
    temporary = None
    binary = None
    try:
        if apply:
            temporary = _temporary_path(output_target)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            binary = os.fdopen(descriptor, "wb")
        with symbol_index._open_guarded_binary(input_target, extra_roots) as handle:
            metadata = os.fstat(handle.fileno())
            if metadata.st_size > limits["max_input_bytes"]:
                raise DataConvertError("input exceeds the input byte ceiling")
            raw = handle.read(limits["max_input_bytes"] + 1)
            if len(raw) > limits["max_input_bytes"]:
                raise DataConvertError("input exceeds the input byte ceiling")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise DataConvertError("input is not valid UTF-8") from exc
            if "\x00" in text:
                raise DataConvertError("input contains NUL bytes and is not accepted as text data")
            sink = _OutputSink(binary, limits["max_output_bytes"])
            row_count, rows = _convert_records(
                _input_records(text, input_format, limits, deadline),
                selected_fields, selected_output_format, sink, limits, deadline,
            )
        if binary is not None:
            binary.flush()
            os.fsync(binary.fileno())
            binary.close()
            binary = None
            _publish_non_overwrite(temporary, output_target, output_root)
            temporary = None
        report = {
            "ok": True,
            "mode": "apply" if apply else "preview",
            "applied": apply,
            "input_path": str(input_target),
            "output_path": str(output_target),
            "input_format": input_format,
            "output_format": selected_output_format,
            "fields": selected_fields,
            "rows": row_count,
            "input_bytes": len(raw),
            "converted_bytes": sink.bytes,
            "converted_sha256": sink.digest.hexdigest(),
            "preview_rows": rows,
            "preview_truncated": row_count > len(rows),
            "limits": limits,
            "report_bytes": 0,
        }
        return _fit_report(report)
    except DataConvertError:
        raise
    except (OSError, PermissionError, TypeError, ValueError, csv.Error) as exc:
        raise DataConvertError("conversion failed safely: %s" % exc) from exc
    finally:
        if binary is not None:
            binary.close()
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
