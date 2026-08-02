"""Hardware-profile NPU detection fields with safe env overrides."""
import system_profile


def test_hardware_profile_defaults_have_no_npu(monkeypatch):
    monkeypatch.setenv("SONDER_NPU_DETECTED", "0")
    monkeypatch.delenv("SONDER_NPU_VENDOR", raising=False)
    monkeypatch.delenv("SONDER_NPU_NAME", raising=False)
    profile = system_profile.detect_hardware()
    assert profile.npu_detected is False
    assert profile.npu_vendor == "none"
    assert profile.npu_name == ""
    assert "npu_detected" in profile.to_dict()


def test_hardware_profile_npu_env_overrides(monkeypatch):
    monkeypatch.setenv("SONDER_NPU_DETECTED", "1")
    monkeypatch.setenv("SONDER_NPU_VENDOR", "amd")
    monkeypatch.setenv("SONDER_NPU_NAME", "AMD Ryzen AI NPU")
    profile = system_profile.detect_hardware()
    assert profile.npu_detected is True
    assert profile.npu_vendor == "amd"
    assert profile.npu_name == "AMD Ryzen AI NPU"


def test_npu_probe_vendor_mapping():
    assert system_profile._npu_vendor_from_name("AMD IPU Device") == "amd"
    assert system_profile._npu_vendor_from_name("Intel(R) AI Boost") == "intel"
    assert (
        system_profile._npu_vendor_from_name("Qualcomm Hexagon NPU")
        == "qualcomm"
    )
    assert system_profile._npu_vendor_from_name("Mystery Neural Unit") == "unknown"
    assert system_profile._npu_vendor_from_pnp_id("PCI\\VEN_1022&DEV_1502") == "amd"


def test_windows_probe_does_not_mistake_input_for_npu(monkeypatch):
    monkeypatch.setattr(system_profile.os, "name", "nt")
    monkeypatch.setenv("SONDER_NPU_PROBE", "1")
    system_profile._NPU_PROBE["value"] = None

    def fake_check_output(command, **kwargs):
        assert "Name,PNPDeviceID" in " ".join(str(part) for part in command)
        return (
            '[{"Name":"Microsoft Input Configuration Device",'
            '"PNPDeviceID":"HID\\\\INPUT"},'
            '{"Name":"NPU Compute Accelerator Device",'
            '"PNPDeviceID":"PCI\\\\VEN_1022&DEV_1502"}]'
        )

    monkeypatch.setattr(
        system_profile.subprocess, "check_output", fake_check_output,
    )
    assert system_profile._npu_probe() == (
        "amd", "NPU Compute Accelerator Device", True,
    )


def test_npu_probe_is_cached_and_disabled_by_env(monkeypatch):
    calls = []
    monkeypatch.setenv("SONDER_NPU_PROBE", "0")
    monkeypatch.delenv("SONDER_NPU_DETECTED", raising=False)
    monkeypatch.delenv("SONDER_NPU_VENDOR", raising=False)
    monkeypatch.delenv("SONDER_NPU_NAME", raising=False)

    def fake_check_output(command, **kwargs):
        rendered = " ".join(str(part) for part in command)
        if "PnPEntity" in rendered:
            calls.append(rendered)
            return "[]"
        # Any other probe (memory, GPU) answers innocuously.
        return '{"total": 16777216, "free": 8388608}'

    monkeypatch.setattr(
        system_profile.subprocess, "check_output", fake_check_output,
    )
    system_profile._NPU_PROBE["value"] = None
    profile = system_profile.detect_hardware()
    assert profile.npu_detected is False
    assert calls == []
