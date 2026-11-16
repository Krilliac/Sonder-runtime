"""Dataset record validation and loading for the security-hypothesis eval."""
from __future__ import annotations

import json

import pytest

from sonder_runtime.domain.security_eval.errors import DatasetError, DatasetRecordError
from sonder_runtime.domain.security_eval.records import (
    LineRange, check_relative_path, collect_records, parse_line_range, parse_record)
from tests.support.security_eval_fixtures import fixture_records, record, write_dataset

pytestmark = pytest.mark.unit


def test_valid_record_round_trips(tmp_path):
    parsed = parse_record(record("h1", tmp_path, lines="13-14"), 1)
    assert parsed.hid == "h1" and parsed.lines == LineRange(13, 14)
    assert parsed.label == "CONFIRMED" and parsed.stage == "r1"
    again = parse_record(parsed.as_dict(), 2)
    assert again == parsed


@pytest.mark.parametrize("value,expected", [
    ("5-9", LineRange(5, 9)), ("7", LineRange(7, 7)), (7, LineRange(7, 7)), ([3, 4], LineRange(3, 4)),
])
def test_line_range_spellings(value, expected):
    assert parse_line_range(value) == expected


@pytest.mark.parametrize("value", ["9-5", "0", "-3", "1-5000", "", "a-b", True, None, [1], [1, 2, 3], 1.5])
def test_bad_line_ranges_are_refused(value):
    with pytest.raises(DatasetRecordError):
        parse_line_range(value)


@pytest.mark.parametrize("path", [
    "../x.cs", "a/../../x.cs", "/etc/passwd", "\\win\\x", "C:/x.cs", "C:x.cs", "a:b", "src/a.cs:stream",
    "\\\\host\\share\\x", "src/\x00a.cs", "", "   ", "./", "src//", "a/..",
])
def test_unsafe_file_fields_are_refused(path):
    with pytest.raises(DatasetRecordError):
        check_relative_path(path)


def test_file_field_is_normalised():
    assert check_relative_path("src\\sub\\a.cs") == "src/sub/a.cs"
    assert check_relative_path("./src/a.cs") == "src/a.cs"


@pytest.mark.parametrize("field,value", [
    ("label", "MAYBE"), ("stage", "r2"), ("category", ""), ("hypothesis", ""), ("hid", ""),
    ("file", "../x"), ("lines", "9-1"), ("repo_root", ""),
])
def test_bad_fields_raise_with_the_line_number(tmp_path, field, value):
    item = record("h1", tmp_path)
    item[field] = value
    with pytest.raises(DatasetRecordError) as info:
        parse_record(item, 41)
    assert info.value.line_no == 41


def test_missing_and_extra_types_are_refused(tmp_path):
    item = record("h1", tmp_path)
    del item["hypothesis"]
    with pytest.raises(DatasetRecordError):
        parse_record(item, 3)
    with pytest.raises(DatasetRecordError):
        parse_record(["not", "an", "object"], 3)


def test_collect_records_keeps_valid_lines_and_reports_invalid_and_duplicates(tmp_path):
    items = fixture_records(tmp_path)
    lines = [(i + 1, json.dumps(item)) for i, item in enumerate(items)]
    lines.append((11, "{not json"))
    lines.append((12, json.dumps(items[0])))
    lines.append((13, "   "))
    records, invalid = collect_records(lines)
    assert [r.hid for r in records] == [item["hid"] for item in items]
    assert sorted(entry["line"] for entry in invalid) == [11, 12]
    duplicate = next(entry for entry in invalid if entry["line"] == 12)
    assert duplicate["hid"] == "h01" and "duplicate" in duplicate["reason"]


def test_collect_records_requires_a_valid_record(tmp_path):
    with pytest.raises(DatasetError):
        collect_records([(1, "{bad")])


def test_loader_reports_invalid_lines_and_keeps_valid_ones(tmp_path):
    from sonder_runtime.adapters.security_eval.dataset import load_dataset
    path = write_dataset(tmp_path / "set.jsonl", fixture_records(tmp_path),
                         extra_lines=["{broken", json.dumps({"hid": "x"})])
    loaded = load_dataset(str(path), max_bytes=1 << 20, max_records=100)
    assert len(loaded.records) == 10
    assert len(loaded.invalid) == 2 and loaded.sha256


def test_loader_bounds(tmp_path):
    from sonder_runtime.adapters.security_eval.dataset import load_dataset
    path = write_dataset(tmp_path / "set.jsonl", fixture_records(tmp_path))
    with pytest.raises(DatasetError):
        load_dataset(str(path), max_bytes=10, max_records=100)
    with pytest.raises(DatasetError):
        load_dataset(str(path), max_bytes=1 << 20, max_records=3)
    with pytest.raises(DatasetError):
        load_dataset(str(tmp_path / "missing.jsonl"), max_bytes=1 << 20, max_records=3)
    with pytest.raises(DatasetError):
        load_dataset("", max_bytes=1 << 20, max_records=3)
