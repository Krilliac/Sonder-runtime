"""domain/common safe_xml and bounded_json: hostile-input bounds (SEC-008)."""
from __future__ import annotations

import json
import time

import pytest

from sonder_runtime.domain.common.bounded_json import (
    JsonBoundsExceeded, check_depth, iter_array_objects, loads_bounded,
)
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.common.safe_xml import UnsafeXml, local_tag, parse_guarded_xml


# ---------------------------------------------------------------- safe_xml

def test_parse_guarded_xml_accepts_plain_document():
    root = parse_guarded_xml(b"<a><b x='1'>t</b></a>", max_bytes=1024)
    assert root.tag == "a" and root.find("b").get("x") == "1"


@pytest.mark.parametrize("payload", [
    b"<!DOCTYPE a [<!ENTITY x 'y'>]><a>&x;</a>",
    b"<?xml version='1.0'?>\n<!doctype a SYSTEM 'http://127.0.0.1:9/x'><a/>",
    b"<!ENTITY x SYSTEM 'file:///etc/passwd'>",
])
def test_parse_guarded_xml_refuses_declarations(payload):
    with pytest.raises(UnsafeXml):
        parse_guarded_xml(payload, max_bytes=4096)


def test_parse_guarded_xml_refuses_utf16_doctype():
    payload = "<?xml version='1.0' encoding='utf-16'?><!DOCTYPE a [<!ENTITY x 'y'>]><a>&x;</a>"
    with pytest.raises(UnsafeXml):
        parse_guarded_xml(payload.encode("utf-16"), max_bytes=4096)


def test_parse_guarded_xml_size_cap_and_malformed_and_type():
    with pytest.raises(UnsafeXml):
        parse_guarded_xml(b"<a>" + b"x" * 100 + b"</a>", max_bytes=50)
    with pytest.raises(UnsafeXml):
        parse_guarded_xml(b"<a><b></a>", max_bytes=100)
    with pytest.raises(UnsafeXml):
        parse_guarded_xml("<a/>", max_bytes=100)  # type: ignore[arg-type]
    assert issubclass(UnsafeXml, InvalidInput)


def test_local_tag():
    assert local_tag("{urn:x}frame") == "frame"
    assert local_tag(None) == ""


# ------------------------------------------------------------ bounded_json

def test_check_depth_ignores_strings_and_refuses_deep():
    assert check_depth('{"a": "[[[[[[[[", "b": [1, [2]]}') == 3
    with pytest.raises(JsonBoundsExceeded):
        check_depth("[" * 65 + "]" * 65, 64)
    assert check_depth("[" * 64 + "]" * 64, 64) == 64


def test_loads_bounded_depth_bomb_refused_fast():
    bomb = "[" * 10_000 + "]" * 10_000
    started = time.monotonic()
    with pytest.raises(JsonBoundsExceeded):
        loads_bounded(bomb, max_bytes=1 << 20)
    assert time.monotonic() - started < 1.0


def test_loads_bounded_huge_digit_string_refused():
    text = '{"n": ' + "9" * 10_000_000 + "}"
    started = time.monotonic()
    with pytest.raises(JsonBoundsExceeded):
        loads_bounded(text, max_bytes=16 << 20)
    assert time.monotonic() - started < 2.0


def test_loads_bounded_byte_cap_and_million_array_bounded():
    million = "[" + ",".join("0" for _ in range(1_000_000)) + "]"
    with pytest.raises(JsonBoundsExceeded):
        loads_bounded(million, max_bytes=1 << 20)
    assert len(loads_bounded(million, max_bytes=4 << 20)) == 1_000_000


def test_loads_bounded_bytes_and_bom_and_invalid():
    assert loads_bounded(b"\xef\xbb\xbf{\"a\": 1}", max_bytes=64) == {"a": 1}
    with pytest.raises(JsonBoundsExceeded):
        loads_bounded(b"\xff\xfe{", max_bytes=64)
    with pytest.raises(JsonBoundsExceeded):
        loads_bounded("{'a': 1}", max_bytes=64)


def _chunks(text: str, size: int):
    return [text[i:i + size] for i in range(0, len(text), size)]


@pytest.mark.parametrize("size", [1, 3, 7, 64, 100_000])
def test_iter_array_objects_trace_events_across_chunk_sizes(size):
    events = [{"name": "Frame", "ph": "X", "ts": i, "args": {"s": "}]{[\"\\"}} for i in range(20)]
    doc = json.dumps({"otherData": {"traceEvents": [1]}, "k": "traceEvents",
                      "traceEvents": events, "tail": [1, 2]})
    scan = iter_array_objects(_chunks(doc, size), max_items=1000)
    got = list(scan)
    assert got == events
    assert scan.found and scan.complete and not scan.truncated
    assert scan.skipped_oversize == 0 and scan.skipped_invalid == 0


def test_iter_array_objects_bare_array_and_bytes_chunks_split_utf8():
    doc = json.dumps([{"name": "é€"}, 5, [1], {"name": "b"}], ensure_ascii=False).encode("utf-8")
    scan = iter_array_objects([doc[i:i + 1] for i in range(len(doc))], max_items=10)
    assert [e["name"] for e in scan] == ["é€", "b"]
    assert scan.skipped_invalid == 1  # the nested array; scalars are ignored


def test_iter_array_objects_oversize_skipped_and_counted_and_max_items():
    big = {"name": "x" * 200_000}
    doc = json.dumps({"traceEvents": [{"a": 1}, big, {"a": 2}, {"a": 3}]})
    scan = iter_array_objects(_chunks(doc, 4096), max_items=2, max_item_bytes=65_536)
    got = list(scan)
    assert got == [{"a": 1}, {"a": 2}]
    assert scan.skipped_oversize == 1 and scan.truncated


def test_iter_array_objects_refuses_non_json_and_deep_outer():
    with pytest.raises(JsonBoundsExceeded):
        list(iter_array_objects(["hello"], max_items=5))
    with pytest.raises(JsonBoundsExceeded):
        list(iter_array_objects(['{"x":' + "[" * 10_000], max_items=5))


def test_iter_array_objects_element_depth_bomb_is_skipped():
    doc = '[{"a": ' + "[" * 5000 + "]" * 5000 + '}, {"b": 1}]'
    scan = iter_array_objects([doc], key=None, max_items=5)
    assert list(scan) == [{"b": 1}]
    assert scan.skipped_invalid == 1


@pytest.mark.parametrize("text", [
    '"' + '\\"' * 400_000,            # unterminated string of escaped quotes
    '["' + '\\"' * 400_000 + ']',
    '{"a":' + '"x",' * 200_000,
], ids=["escaped_quotes", "escaped_quotes_in_array", "many_strings"])
def test_check_depth_is_linear_on_hostile_strings(text):
    started = time.perf_counter()
    try:
        check_depth(text)
    except JsonBoundsExceeded:
        pass
    assert time.perf_counter() - started < 1.0


def test_check_depth_ignores_brackets_in_strings_and_counts_real_ones():
    assert check_depth('["[[[[", {"k": "}}}"}, "\\"[", ["x"]]') == 2
    assert check_depth('["unterminated [[[[[[') == 1
    with pytest.raises(JsonBoundsExceeded):
        check_depth('[' * 65 + ']' * 65)
    assert check_depth(']]]]' + '[' * 64) == 64
