"""Ownership and compatibility tests for the packaged system profile."""

import importlib


def test_root_import_is_the_canonical_module():
    legacy = importlib.import_module("system_profile")
    canonical = importlib.import_module("sonder_runtime.platform.system_profile")

    assert legacy is canonical
    assert legacy.HardwareProfile is canonical.HardwareProfile


def test_legacy_monkeypatch_changes_canonical_profile_path(monkeypatch, tmp_path):
    legacy = importlib.import_module("system_profile")
    canonical = importlib.import_module("sonder_runtime.platform.system_profile")
    monkeypatch.setattr(legacy, "workspace_root", lambda: str(tmp_path))

    assert canonical.workspace_root() == str(tmp_path)
    assert canonical.write_profile("portable") == str(tmp_path / "system_profile.md")
    assert canonical.read_profile() == "portable"


def test_canonical_profile_preserves_hardware_override(monkeypatch):
    profile = importlib.import_module("sonder_runtime.platform.system_profile")
    monkeypatch.setenv("SONDER_RAM_GB", "32")
    monkeypatch.setenv("SONDER_AVAILABLE_RAM_GB", "12")
    monkeypatch.setenv("SONDER_NPU_DETECTED", "true")
    monkeypatch.setenv("SONDER_NPU_VENDOR", "intel")
    monkeypatch.setenv("SONDER_NPU_NAME", "AI Boost")
    monkeypatch.setattr(profile, "_nvidia_profile", lambda gpu_index=None: None)
    monkeypatch.setattr(profile, "_rocm_profile", lambda: None)

    detected = profile.detect_hardware()

    assert detected.system_ram_total_gb == 32.0
    assert detected.system_ram_available_gb == 12.0
    assert detected.npu_vendor == "intel"
    assert detected.npu_name == "AI Boost"
    assert detected.npu_detected is True
