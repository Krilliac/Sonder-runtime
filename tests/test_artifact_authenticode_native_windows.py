"""Windows canary for the real Authenticode verifier and exact publisher pin."""

import os
from pathlib import Path

import pytest

from sonder_runtime.adapters import artifact_fetch

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires native Windows Authenticode")


def test_signed_windows_powershell_matches_exact_microsoft_organization(monkeypatch):
    # This binary ships with Windows and is independent of downloaded fixtures
    # and this repository's Python installer. Never infer the expected signer
    # from the binary's metadata: the publisher pin is a fixed host expectation.
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    assert system_root, "Windows system root is unavailable"
    executable = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    assert executable.is_file(), f"Windows PowerShell is unavailable: {executable}"

    # Keep the production filesystem guard active while authorizing this one
    # host-supplied directory for the test; use the trusted verifier executable
    # selected by the Windows system root, not an override or PATH lookup.
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(executable.parent))
    monkeypatch.delenv("SONDER_POWERSHELL", raising=False)
    assert Path(artifact_fetch._powershell_executable()).resolve() == executable.resolve()
    result = artifact_fetch.verify_artifact(
        str(executable), expect_type="pe", expect_publisher="Microsoft Corporation",
    )

    # pytest truncates a large dict in the failure line; name the fields that
    # say *why* a verdict was not "verified" so a CI failure is diagnosable.
    detail = {key: result.get(key) for key in ("verdict", "signature", "checks")}
    assert result["detected_type"] == "pe", detail
    assert result["signature_verified"] is True, detail
    assert result["signature"]["supported"] is True, detail
    assert result["signature"]["status"] == "Valid", detail
    assert result["signature"]["thumbprint"], detail
    assert artifact_fetch._signer_organization(result["signature"]["publisher"]) == "Microsoft Corporation"
    assert result["ok"] and result["verdict"] == "verified", detail
    assert {row["check"] for row in result["checks"] if row["ok"]} >= {
        "size", "block_page", "magic", "publisher",
    }


def test_unicode_quote_in_path_cannot_inject_powershell(monkeypatch, tmp_path):
    # PowerShell treats U+2019 as a single-quote delimiter. Were the path
    # quoted into the -Command text, this name would close the string and
    # the $(...) subexpression would create the marker file in the working
    # directory. (No colon in the name: NTFS reads one as a stream separator.)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SONDER_POWERSHELL", raising=False)
    marker = tmp_path / "sonder-inject-marker"
    target = tmp_path / "a\u2019+$(ni sonder-inject-marker)+\u2019.exe"
    target.write_bytes(b"MZ" + b"\x00" * 64)

    signature = artifact_fetch._authenticode_signature(target)

    assert not marker.exists(), signature
    # The verifier saw the real file under its literal name.
    assert signature["supported"] is True, signature
    assert signature["status"] != "Valid", signature
