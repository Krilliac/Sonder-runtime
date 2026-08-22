"""Pure policy for identifying client NPU hardware."""
import re


NPU_NAME_RE = re.compile(
    r"(?:\bnpu\b|neural\s+processing\s+unit|ai boost|ryzen\s*ai)",
    re.IGNORECASE,
)


def vendor_from_name(name: object) -> str:
    lowered = str(name or "").lower()
    if "amd" in lowered or "ryzen" in lowered:
        return "amd"
    if "intel" in lowered or "ai boost" in lowered:
        return "intel"
    if "qualcomm" in lowered or "hexagon" in lowered:
        return "qualcomm"
    return "unknown"


def vendor_from_pnp_id(pnp_device_id: object) -> str:
    value = str(pnp_device_id or "").upper()
    if "VEN_1022" in value:
        return "amd"
    if "VEN_8086" in value:
        return "intel"
    if "VEN_17CB" in value:
        return "qualcomm"
    return "unknown"


def linux_accel_is_npu(vendor: object, driver: object) -> bool:
    """Accept only accelerator drivers tied to client NPU device classes."""
    supported = {
        "amd": {"amdxdna"},
        "intel": {"intel_vpu", "ivpu"},
    }
    vendor_id = str(vendor or "").lower()
    return str(driver or "").lower() in supported.get(vendor_id, set())
