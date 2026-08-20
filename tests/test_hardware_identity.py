from sonder_runtime.platform.hardware_identity import looks_integrated, vendor_from_text
import sonder_hardware


def test_vendor_policy_is_owned_by_platform_boundary():
    assert sonder_hardware._vendor_from_text is not vendor_from_text
    assert sonder_hardware._vendor_from_text("NVIDIA Corporation") == vendor_from_text(
        "NVIDIA Corporation"
    )


def test_vendor_policy_recognizes_pci_and_display_names():
    assert vendor_from_text("VEN_10DE") == "NVIDIA"
    assert vendor_from_text("Advanced Micro Devices, Inc.") == "AMD"
    assert vendor_from_text("VEN_8086") == "Intel"
    assert vendor_from_text("Apple M2") == "Apple"


def test_vendor_policy_is_conservative_for_unknown_values():
    assert vendor_from_text(None, "mystery adapter") == "unknown"


def test_integrated_policy_is_owned_by_platform_boundary():
    assert sonder_hardware._looks_integrated is not looks_integrated
    assert sonder_hardware._looks_integrated("Intel UHD Graphics 770", "Intel") is True
    assert looks_integrated("NVIDIA GeForce RTX 5070 Ti", "NVIDIA") is False


def test_integrated_policy_handles_discrete_and_unknown_families():
    assert looks_integrated("Intel Arc A770", "Intel") is False
    assert looks_integrated("AMD Radeon RX 7900", "AMD") is False
    assert looks_integrated("AMD Radeon 780M", "AMD") is True
    assert looks_integrated("mystery adapter", "unknown") is None
