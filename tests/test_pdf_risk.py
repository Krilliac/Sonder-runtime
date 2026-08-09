"""Adversarial tests for bounded, non-executing PDF risk inspection."""
from __future__ import annotations

import json
import os
import zlib

import pytest

import file_ops
import pdf_risk


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
    assert result["unsupported_filters"] == ["LZWDecode"]
    assert "unsupported_stream_filter" in result["incomplete_reasons"]


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
