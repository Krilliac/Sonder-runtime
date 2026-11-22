"""Adversarial tests for bounded, non-executing PDF risk inspection."""
from __future__ import annotations

import json
import os
import time
import zlib

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.pdf_risk as pdf_risk


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    return root


def _pdf(body=b"<< /Type /Catalog >>"):
    return b"%PDF-1.7\n1 0 obj\n" + body + b"\nendobj\n%%EOF\n"


def _features(result):
    return {row["feature"]: row for row in result["findings"]}


def test_passive_minimal_pdf_is_complete_and_deterministic(project):
    path = project / "plain.pdf"
    path.write_bytes(_pdf())
    first = pdf_risk.inspect_pdf(path)
    second = pdf_risk.inspect_pdf(path)
    assert pdf_risk.format_result(first) == pdf_risk.format_result(second)
    assert first["scan_complete"] is True
    assert first["risk"] == "none_detected"
    assert first["sha256"]
    assert first["execution"] == "none"


def test_raw_active_actions_are_high_risk(project):
    path = project / "active.pdf"
    path.write_bytes(_pdf(
        b"<< /Type /Catalog /OpenAction 2 0 R /AA << /O 3 0 R >> "
        b"/Names << /EmbeddedFiles 4 0 R >> >>\n"
        b"2 0 obj << /S /JavaScript /JS (app.alert('x')) >> endobj\n"
        b"3 0 obj << /S /Launch /F (payload.exe) >> endobj"
    ))
    result = pdf_risk.inspect_pdf(path)
    features = _features(result)
    assert result["risk"] == "high"
    assert {"javascript", "open_action", "additional_actions", "launch_action", "embedded_file"} <= set(features)
    assert all("raw" in row["locations"] for row in features.values())


def test_pdf_name_hex_escapes_do_not_hide_javascript(project):
    path = project / "escaped.pdf"
    path.write_bytes(_pdf(b"<< /S /Java#53cript /J#53 (evil) >>"))
    result = pdf_risk.inspect_pdf(path)
    assert result["risk"] == "high"
    assert "javascript" in _features(result)


def test_flate_object_stream_is_decoded_under_cap(project):
    payload = b"2 0 obj << /S /JavaScript /JS (hidden) /URI (http://example.invalid) >> endobj"
    compressed = zlib.compress(payload)
    body = (
        b"<< /Length " + str(len(compressed)).encode() + b" /Filter /FlateDecode /Type /ObjStm >>\n"
        b"stream\n" + compressed + b"\nendstream"
    )
    path = project / "compressed.pdf"
    path.write_bytes(_pdf(body))
    result = pdf_risk.inspect_pdf(path)
    assert result["scan_complete"] is True
    assert result["streams_decoded"] == 1
    assert _features(result)["javascript"]["locations"] == ["decoded_stream"]
    assert "external_uri" in _features(result)


def test_unsupported_filter_makes_clean_result_unknown(project):
    path = project / "unsupported.pdf"
    path.write_bytes(_pdf(b"<< /Filter /LZWDecode >>\nstream\nopaque\nendstream"))
    result = pdf_risk.inspect_pdf(path)
    assert result["scan_complete"] is False
    assert result["risk"] == "unknown"
    assert result["unsupported_filter_count"] == 1
    assert "unsupported_stream_filter" in result["incomplete_reasons"]


def test_unsupported_filter_name_is_never_returned(project):
    canary = "TOPSECRETFILTERCANARY"
    path = project / "private-filter.pdf"
    path.write_bytes(_pdf(
        b"<< /Filter /" + canary.encode() + b" >>\nstream\nopaque\nendstream"
    ))
    output = pdf_risk.format_result(pdf_risk.inspect_pdf(path))
    assert canary not in output
    assert '"unsupported_filter_count":1' in output


def test_concatenated_flate_members_never_report_clean(project):
    compressed = zlib.compress(b"benign") + zlib.compress(b"/JavaScript /OpenAction")
    path = project / "concat.pdf"
    path.write_bytes(_pdf(
        b"<< /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream"
    ))
    result = pdf_risk.inspect_pdf(path)
    assert result["scan_complete"] is False
    assert result["risk"] == "unknown"
    assert "malformed_flate_stream" in result["incomplete_reasons"]


def test_deadline_overrun_cannot_report_complete(project, monkeypatch):
    compressed = zlib.compress(b"benign")
    path = project / "slow.pdf"
    path.write_bytes(_pdf(
        b"<< /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream"
    ))
    original = pdf_risk._flate_decode

    def slow_decode(*args, **kwargs):
        time.sleep(0.06)
        return original(*args, **kwargs)

    monkeypatch.setattr(pdf_risk, "_flate_decode", slow_decode)
    with pytest.raises(TimeoutError):
        pdf_risk.inspect_pdf(path, max_seconds=0.05)


def test_encryption_never_reports_clean(project):
    path = project / "encrypted.pdf"
    path.write_bytes(_pdf(b"<< /Encrypt 2 0 R >>"))
    result = pdf_risk.inspect_pdf(path)
    assert result["scan_complete"] is False
    assert result["risk"] == "unknown"
    assert "encrypted_content" in result["incomplete_reasons"]


def test_partial_scan_is_explicit_and_hash_is_withheld(project):
    path = project / "large.pdf"
    path.write_bytes(b"%PDF-1.7\n" + b"A" * 3000 + b"%%EOF\n")
    result = pdf_risk.inspect_pdf(path, max_scan_bytes=1024)
    assert result["scan_complete"] is False
    assert result["risk"] == "unknown"
    assert result["sha256"] is None
    assert result["ranges_scanned"] == [[0, 512], [2503, 3015]]


def test_decode_bomb_is_bounded_and_incomplete(project):
    compressed = zlib.compress(b"/JavaScript " + b"A" * 500_000)
    path = project / "bomb.pdf"
    path.write_bytes(_pdf(
        b"<< /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream"
    ))
    result = pdf_risk.inspect_pdf(path, max_decoded_bytes=1024)
    assert result["decoded_bytes"] == 1024
    assert result["scan_complete"] is False
    assert result["risk"] == "high"
    assert "decoded_byte_limit" in result["incomplete_reasons"]


def test_invalid_header_and_empty_file_are_rejected(project):
    empty = project / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(pdf_risk.PdfRiskError, match="empty"):
        pdf_risk.inspect_pdf(empty)
    fake = project / "fake.pdf"
    fake.write_bytes(b"not a pdf")
    with pytest.raises(pdf_risk.PdfRiskError, match="header"):
        pdf_risk.inspect_pdf(fake)


def test_sensitive_outside_and_symlink_paths_are_rejected(project, tmp_path):
    sensitive = project / ".ssh"
    sensitive.mkdir()
    (sensitive / "document.pdf").write_bytes(_pdf())
    with pytest.raises(PermissionError):
        pdf_risk.inspect_pdf(sensitive / "document.pdf")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(_pdf())
    with pytest.raises(PermissionError):
        pdf_risk.inspect_pdf(outside)
    if hasattr(os, "symlink"):
        link = project / "link.pdf"
        try:
            link.symlink_to(outside)
        except OSError:
            return
        with pytest.raises(PermissionError):
            pdf_risk.inspect_pdf(link)


def test_json_format_is_transport_safe(project):
    path = project / ("unicode-" + chr(0xDCFF) + ".pdf")
    try:
        path.write_bytes(_pdf())
    except (OSError, UnicodeEncodeError):
        pytest.skip("filesystem does not support surrogate test names")
    encoded = pdf_risk.format_result(pdf_risk.inspect_pdf(path))
    assert json.loads(encoded)["risk"] == "none_detected"
    encoded.encode("ascii")


def _hidden_javascript_stream(dictionary, separator=b"\n"):
    compressed = zlib.compress(b"2 0 obj << /S /JavaScript /JS (hidden) >> endobj")
    return dictionary + separator + b"stream\n" + compressed + b"\nendstream"


def test_stream_keyword_directly_after_dictionary_is_decoded(project):
    path = project / "no-eol.pdf"
    path.write_bytes(_pdf(_hidden_javascript_stream(b"<< /Filter /FlateDecode >>", b"")))
    result = pdf_risk.inspect_pdf(path)
    assert result["streams_decoded"] == 1
    assert result["risk"] == "high"
    assert _features(result)["javascript"]["locations"] == ["decoded_stream"]


def test_nested_decode_parms_dictionary_keeps_filter(project):
    path = project / "nested.pdf"
    path.write_bytes(_pdf(_hidden_javascript_stream(
        b"<< /Filter /FlateDecode /DecodeParms << /Columns 4 >> /Length 99 >>"
    )))
    result = pdf_risk.inspect_pdf(path)
    assert result["streams_decoded"] == 1
    assert result["risk"] == "high"
    assert _features(result)["javascript"]["locations"] == ["decoded_stream"]


def test_literal_string_delimiters_do_not_misplace_dictionary(project):
    path = project / "string.pdf"
    path.write_bytes(_pdf(_hidden_javascript_stream(
        b"<< /Filter /FlateDecode /T (a << b \\) c) /Length 99 >>"
    )))
    result = pdf_risk.inspect_pdf(path)
    assert result["streams_decoded"] == 1
    assert result["risk"] == "high"


@pytest.mark.parametrize("dictionary", [
    b"",
    b"<< /Filter /FlateDecode %<<\n /Length 99 >>",
    b"<< /Filter /FlateDecode /DecodeParms << %<<\n >> >>",
    b"/Filter /FlateDecode /Length 99 >>",
    b"<< /X << /Filter /FlateDecode >> /Filter [/FlateDecode /FlateDecode] >>",
])
def test_unlocatable_stream_dictionary_never_reports_clean(project, dictionary):
    compressed = zlib.compress(b"benign")
    path = project / "ambiguous.pdf"
    path.write_bytes(_pdf(dictionary + b"\nstream\n" + compressed + b"\nendstream"))
    result = pdf_risk.inspect_pdf(path)
    assert result["scan_complete"] is False
    assert result["risk"] == "unknown"
    assert "ambiguous_stream_dictionary" in result["incomplete_reasons"]


def test_invalid_zlib_header_is_reported_as_malformed_stream(project):
    path = project / "bad-zlib.pdf"
    path.write_bytes(_pdf(
        b"<< /Filter /FlateDecode >>\nstream\nnot zlib data at all\nendstream"
    ))
    result = pdf_risk.inspect_pdf(path)
    assert result["scan_complete"] is False
    assert result["risk"] == "unknown"
    assert result["malformed_streams"] == 1
    assert "malformed_flate_stream" in result["incomplete_reasons"]


def test_many_unterminated_stream_keywords_scan_linearly(project):
    path = project / "unterminated.pdf"
    path.write_bytes(b"%PDF-1.7\n" + b"<< >>\nstream\n" * 16384)
    started = time.monotonic()
    result = pdf_risk.inspect_pdf(path, max_seconds=15)
    assert time.monotonic() - started < 3.0
    assert result["scan_complete"] is False
    assert result["risk"] == "unknown"
    assert "unterminated_stream" in result["incomplete_reasons"]


@pytest.mark.parametrize("after_keyword", [b" \n", b"\t\r\n", b" \x00\x0c\r"])
def test_whitespace_between_stream_keyword_and_eol_is_skipped(project, after_keyword):
    compressed = zlib.compress(b"2 0 obj << /S /JavaScript /JS (hidden) >> endobj")
    path = project / "trailing-space.pdf"
    path.write_bytes(_pdf(
        b"<< /Filter /FlateDecode >>\nstream" + after_keyword + compressed + b"\nendstream"
    ))
    result = pdf_risk.inspect_pdf(path)
    assert result["streams_decoded"] == 1
    assert result["risk"] == "high"
    assert _features(result)["javascript"]["locations"] == ["decoded_stream"]


@pytest.mark.parametrize("after_keyword", [b"", b" ", b"x\n"])
def test_stream_keyword_without_eol_never_reports_clean(project, after_keyword):
    compressed = zlib.compress(b"benign")
    path = project / "no-trailing-eol.pdf"
    path.write_bytes(_pdf(
        b"<< /Filter /FlateDecode >>\nstream" + after_keyword + compressed + b"\nendstream"
    ))
    result = pdf_risk.inspect_pdf(path)
    assert result["scan_complete"] is False
    assert result["risk"] == "unknown"
    assert "stream_keyword_without_eol" in result["incomplete_reasons"]


@pytest.mark.parametrize("separator", [
    b" " * 9000,
    b"\x00" * 9000,
    b"\n" * 9000 + b" ",
    b" %c\n ",
    b"\n%c\n\t",
])
def test_stream_keyword_without_locatable_dictionary_is_not_dropped(project, separator):
    path = project / "separated.pdf"
    path.write_bytes(_pdf(_hidden_javascript_stream(b"<< /Filter /FlateDecode >>", separator)))
    result = pdf_risk.inspect_pdf(path)
    assert result["streams_seen"] == 1
    assert result["scan_complete"] is False
    assert result["risk"] == "unknown"
    assert "ambiguous_stream_dictionary" in result["incomplete_reasons"]


@pytest.mark.parametrize("text", [b"/stream", b"%stream", b"(upstream)", b"(streamline)"])
def test_stream_inside_other_tokens_is_not_a_keyword(project, text):
    path = project / "token.pdf"
    path.write_bytes(_pdf(b"<< /T " + text + b" >>"))
    result = pdf_risk.inspect_pdf(path)
    assert result["streams_seen"] == 0
    assert result["scan_complete"] is True
    assert result["risk"] == "none_detected"


def _flate_js_obj(dictionary):
    data = zlib.compress(b"<< /S /JavaScript /JS (app.alert(1)) >>")
    return b"2 0 obj\n" + dictionary + b"\nstream\n" + data + b"\nendstream\nendobj"


@pytest.mark.parametrize("decoy", [b"(\n<< >>\nstream\n)", b"(<< >> stream\n)"])
def test_stream_keyword_inside_literal_string_does_not_hide_next_stream(project, decoy):
    path = project / "decoy.pdf"
    body = b"<< /Info " + decoy + b" >>\nendobj\n" + _flate_js_obj(b"<< /Filter /FlateDecode >>")
    path.write_bytes(_pdf(body))
    result = pdf_risk.inspect_pdf(path)
    assert result["risk"] in ("high", "unknown")
    assert result["scan_complete"] is False or "javascript" in _features(result)


@pytest.mark.parametrize("dictionary", [
    b"<< /Filter 5 0 R >>",
    b"<< /Filter\x00/FlateDecode >>",
    b"<< /Filter\x00[/FlateDecode] >>",
])
def test_indirect_or_nul_separated_filter_never_reports_clean(project, dictionary):
    path = project / "filter.pdf"
    path.write_bytes(_pdf(_flate_js_obj(dictionary)))
    result = pdf_risk.inspect_pdf(path)
    assert result["risk"] in ("high", "unknown")
    assert result["scan_complete"] is False or "javascript" in _features(result)


def _hex_flate_obj(dictionary):
    hexed = b"".join(b"%02x" % b for b in b"<< /S /JavaScript /JS (app.alert(1)) >>") + b">"
    data = zlib.compress(hexed)
    return b"2 0 obj\n" + dictionary + b"\nstream\n" + data + b"\nendstream\nendobj"


@pytest.mark.parametrize("dictionary", [
    b"<< /Filter [/FlateDecode 7 0 R] >>",
    b"<< /Filter [7 0 R /FlateDecode] >>",
    b"<< /X << /Filter /FlateDecode >> /Filter 5 0 R >>",
])
def test_indirect_filter_in_array_or_behind_nested_filter_is_flagged(project, dictionary):
    path = project / "arrayfilter.pdf"
    path.write_bytes(_pdf(_hex_flate_obj(dictionary)))
    result = pdf_risk.inspect_pdf(path)
    assert result["scan_complete"] is False
    assert result["risk"] != "none_detected"
    assert set(result["incomplete_reasons"]) & {"unsupported_stream_filter", "ambiguous_stream_dictionary"}
