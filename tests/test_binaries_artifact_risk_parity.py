"""Parity: adapters.artifact_risk and domain.binaries agree on PE/ELF fields.

artifact_risk keeps its own (IO-mixing) parser until it migrates onto the
pure readers (spec F7 / risk 16). This test keeps the two from drifting on
the fields both report.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

import sonder_runtime.adapters.artifact_risk as artifact_risk
import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.domain.binaries.elf_ids import read_elf_header, read_elf_identity
from sonder_runtime.domain.binaries.pe_debug import read_pe_identity
from sonder_runtime.domain.binaries.reader import BytesReader


FIXTURES = Path(__file__).parent / "fixtures" / "crash" / "pe"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    monkeypatch.delenv("SONDER_EXECUTION_RISK_POLICY", raising=False)
    return root


@pytest.mark.parametrize("variant", ["a", "b"])
def test_pe_parity_on_clang_cl_fixtures(project, variant):
    data = (FIXTURES / variant / "spark_tiny.exe").read_bytes()
    path = project / "spark_tiny.exe"
    path.write_bytes(data)
    risk = artifact_risk.inspect_artifact(path)
    pure = read_pe_identity(BytesReader(data))
    assert risk["kind"] == "pe"
    assert risk["details"]["machine"] == pure.machine_code
    assert risk["details"]["characteristics"] == pure.characteristics
    assert len(risk["details"]["sections"]) == pure.sections


def _elf64(machine: int = 0x3E, e_type: int = 2) -> bytes:
    data = bytearray(128)
    data[:16] = b"\x7fELF" + bytes((2, 1, 1)) + b"\x00" * 9
    struct.pack_into("<HHIQQQIHHHHHH", data, 16, e_type, machine, 1, 0x400000, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0x400000, 0x400000, 64, 64, 0x1000)
    return bytes(data)


@pytest.mark.parametrize("machine,e_type", [(0x3E, 2), (183, 3)])
def test_elf_parity_on_synthetic_images(project, machine, e_type):
    data = _elf64(machine, e_type)
    path = project / "tool"
    path.write_bytes(data)
    risk = artifact_risk.inspect_artifact(path)
    header = read_elf_header(BytesReader(data))
    identity = read_elf_identity(BytesReader(data))
    assert risk["details"]["machine"] == header.machine == identity.machine_code
    assert risk["details"]["type"] == header.type == identity.type_code
    assert risk["details"]["bits"] == header.bits
    assert risk["details"]["program_headers"] == header.phnum


@pytest.mark.skipif(shutil.which("g++") is None, reason="needs g++")
def test_elf_parity_on_real_binary(project):
    source = project / "t.cpp"
    source.write_text("int main() { return 0; }\n")
    binary = project / "t"
    subprocess.run(["g++", "-o", str(binary), str(source)], check=True, timeout=120)
    data = binary.read_bytes()
    risk = artifact_risk.inspect_artifact(binary)
    identity = read_elf_identity(BytesReader(data))
    header = read_elf_header(BytesReader(data))
    assert risk["details"]["machine"] == identity.machine_code
    assert risk["details"]["type"] == identity.type_code
    assert risk["details"]["program_headers"] == header.phnum
    assert len(identity.build_id) >= 16
