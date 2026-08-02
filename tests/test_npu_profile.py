"""Hardware-profile NPU detection fields with safe env overrides."""
import io
import sys

import system_profile


def test_windows_memory_fallback_never_spawns_powershell(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(system_profile.os, "name", "nt")
    monkeypatch.setattr(
        system_profile, "_windows_system_memory", lambda: (16.0, 8.0, True),
    )
    monkeypatch.setattr(
        system_profile.subprocess, "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Windows RAM fallback must not spawn PowerShell")
        ),
    )
    assert system_profile._system_memory() == (16.0, 8.0, True)


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


def test_lightweight_npu_detection_does_not_run_full_hardware_probes(
        monkeypatch):
    monkeypatch.setenv("SONDER_NPU_DETECTED", "1")
    monkeypatch.setenv("SONDER_NPU_VENDOR", "intel")
    monkeypatch.setenv("SONDER_NPU_NAME", "Intel AI Boost")
    monkeypatch.setattr(
        system_profile, "_system_memory",
        lambda: (_ for _ in ()).throw(AssertionError("RAM probe must not run")),
    )
    for helper in ("_nvidia_profile", "_rocm_profile"):
        monkeypatch.setattr(
            system_profile, helper,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("GPU probe must not run")
            ),
        )
    assert system_profile.detect_npu_hardware() == (
        "intel", "Intel AI Boost", True,
    )


def test_npu_probe_vendor_mapping():
    assert system_profile._npu_vendor_from_name("AMD IPU Device") == "amd"
    assert system_profile._npu_vendor_from_name("Intel(R) AI Boost") == "intel"
    assert (
        system_profile._npu_vendor_from_name("Qualcomm Hexagon NPU")
        == "qualcomm"
    )
    assert system_profile._npu_vendor_from_name("Mystery Neural Unit") == "unknown"
    assert system_profile._npu_vendor_from_pnp_id("PCI\\VEN_1022&DEV_1502") == "amd"


def test_linux_qaic_cloud_accelerator_is_not_classified_as_npu():
    assert system_profile._linux_accel_is_npu("qualcomm", "qaic") is False
    assert system_profile._linux_accel_is_npu("amd", "amdxdna") is True
    assert system_profile._linux_accel_is_npu("intel", "ivpu") is True
    assert system_profile._linux_accel_is_npu("qualcomm", "ivpu") is False
    assert system_profile._linux_accel_is_npu("intel", "amdxdna") is False


def test_linux_probe_checks_bounded_nodes_until_strict_npu_match(monkeypatch):
    vendors = {
        "accel0": "0x17cb",  # QAIC: generic cloud accelerator, not this NPU path
        "accel1": "0x8086",  # Intel client NPU
    }

    def fake_open(path, *_args, **_kwargs):
        node = str(path).split("/")[4]
        return io.StringIO(vendors[node])

    def fake_realpath(path):
        node = str(path).split("/")[4]
        driver = "qaic" if node == "accel0" else "ivpu"
        return "/sys/bus/pci/drivers/%s" % driver

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(system_profile.os.path, "realpath", fake_realpath)
    assert system_profile._linux_npu_from_nodes(["accel0", "accel1"]) == (
        "intel", "accel1", True,
    )


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


def test_windows_probe_rejects_generic_image_and_video_units(monkeypatch):
    monkeypatch.setattr(system_profile.os, "name", "nt")
    monkeypatch.setenv("SONDER_NPU_PROBE", "1")
    system_profile._NPU_PROBE["value"] = None
    monkeypatch.setattr(
        system_profile.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (
            '[{"Name":"Intel Imaging IPU Device",'
            '"PNPDeviceID":"PCI\\\\VEN_8086&DEV_0001"},'
            '{"Name":"Generic Video VPU",'
            '"PNPDeviceID":"PCI\\\\VEN_8086&DEV_0002"}]'
        ),
    )
    assert system_profile._npu_probe() == ("none", "", False)


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
