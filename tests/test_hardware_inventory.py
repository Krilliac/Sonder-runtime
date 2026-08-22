import sonder_hardware

from sonder_runtime.adapters.accelerators.inventory import dedupe_accelerators


def test_accelerator_dedupe_has_packaged_owner():
    assert sonder_hardware._dedupe_accelerators is dedupe_accelerators


def test_accelerator_dedupe_keeps_distinct_devices_and_removes_exact_ids():
    first = {
        "name": "NVIDIA GPU", "vendor": "NVIDIA", "memory_gb": 16.0,
        "integrated": False, "probe": "nvidia-smi", "device_id": "GPU-1",
    }
    duplicate = dict(first)
    second = dict(first, device_id="GPU-2")

    assert dedupe_accelerators([first, duplicate, second]) == [first, second]


def test_accelerator_dedupe_uses_identity_fields_without_device_id():
    first = {
        "name": "Intel UHD", "vendor": "Intel", "memory_gb": None,
        "integrated": True, "probe": "linux-drm-sysfs", "device_id": "",
    }
    duplicate = dict(first)
    distinct = dict(first, name="Intel Arc A770", integrated=False)

    assert dedupe_accelerators([first, duplicate, distinct]) == [first, distinct]
