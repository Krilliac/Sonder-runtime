from sonder_runtime.platform import hardware_identity
import sonder_hardware
from sonder_runtime.adapters.accelerators.gpu_probe import probe_nvidia_gpu
from sonder_runtime.domain.model_sizing import band_for_capacity, largest_model_class
from sonder_runtime.platform.hardware_probe import probe_platform


def test_vendor_policy_is_owned_by_platform_boundary():
    assert sonder_hardware._vendor_from_text is not hardware_identity.vendor_from_text
    assert sonder_hardware._vendor_from_text("NVIDIA Corporation") == hardware_identity.vendor_from_text(
        "NVIDIA Corporation"
    )


def test_vendor_policy_recognizes_pci_and_display_names():
    assert hardware_identity.vendor_from_text("VEN_10DE") == "NVIDIA"
    assert hardware_identity.vendor_from_text("Advanced Micro Devices, Inc.") == "AMD"
    assert hardware_identity.vendor_from_text("VEN_8086") == "Intel"
    assert hardware_identity.vendor_from_text("Apple M2") == "Apple"


def test_vendor_policy_is_conservative_for_unknown_values():
    assert hardware_identity.vendor_from_text(None, "mystery adapter") == "unknown"


def test_integrated_policy_is_owned_by_platform_boundary():
    assert sonder_hardware._looks_integrated is not hardware_identity.looks_integrated
    assert sonder_hardware._looks_integrated("Intel UHD Graphics 770", "Intel") is True
    assert hardware_identity.looks_integrated("NVIDIA GeForce RTX 5070 Ti", "NVIDIA") is False


def test_integrated_policy_handles_discrete_and_unknown_families():
    assert hardware_identity.looks_integrated("Intel Arc A770", "Intel") is False
    assert hardware_identity.looks_integrated("AMD Radeon RX 7900", "AMD") is False
    assert hardware_identity.looks_integrated("AMD Radeon 780M", "AMD") is True
    assert hardware_identity.looks_integrated("mystery adapter", "unknown") is None


def test_accelerator_record_owns_normalized_record_construction():
    assert sonder_hardware._accelerator is hardware_identity.accelerator_record


def test_accelerator_record_normalizes_defaults_and_memory():
    record = hardware_identity.accelerator_record(
        name="NVIDIA RTX", vendor="NVIDIA", memory_gb=15.96,
        memory_kind="dedicated VRAM", probe="fixture",
    )

    assert record == {
        "name": "NVIDIA RTX",
        "vendor": "NVIDIA",
        "memory_gb": 16.0,
        "memory_kind": "dedicated VRAM",
        "integrated": False,
        "probe": "fixture",
        "device_id": "",
        "presence_verified": True,
        "runtime_ready": None,
    }


def test_accelerator_record_keeps_explicit_unknown_values_conservative():
    record = hardware_identity.accelerator_record(
        name="mystery", vendor="unknown", memory_gb=0,
        integrated="maybe", probe="fixture", presence_verified="unknown",
    )

    assert record["name"] == "mystery"
    assert record["memory_gb"] is None
    assert record["integrated"] is None
    assert record["presence_verified"] is None
    assert record["runtime_ready"] is None


def test_hardware_boundaries_keep_accelerator_and_platform_probes_packaged():
    assert sonder_hardware._probe_gpu is probe_nvidia_gpu
    assert sonder_hardware._probe_platform is probe_platform


def test_model_sizing_helpers_own_remaining_root_capacity_policy():
    assert sonder_hardware._band_for is band_for_capacity
    assert sonder_hardware._largest_model_class is largest_model_class
    assert sonder_hardware._band_for(8.0, ((10.0, "small"), (float("inf"), "large"))) == "small"
    assert sonder_hardware._largest_model_class(20.0) == largest_model_class(20.0)
    assert band_for_capacity(10.0, ((10.0, "small"), (float("inf"), "large"))) == "large"
