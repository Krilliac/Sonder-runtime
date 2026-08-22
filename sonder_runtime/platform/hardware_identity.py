"""Platform identity normalization for enumerated hardware."""
from __future__ import annotations


def vendor_from_text(*values: object) -> str:
    """Normalize display-adapter vendor text without claiming backend support."""
    text = " ".join(str(value or "") for value in values).lower()
    if "nvidia" in text or "ven_10de" in text:
        return "NVIDIA"
    if (
        "advanced micro devices" in text
        or "amd" in text
        or "ati " in text
        or "ven_1002" in text
    ):
        return "AMD"
    if "intel" in text or "ven_8086" in text:
        return "Intel"
    if "apple" in text or "ven_106b" in text:
        return "Apple"
    return "unknown"


def looks_integrated(name: str, vendor: str) -> bool | None:
    """Classify whether an enumerated display adapter is integrated."""
    lowered = (name or "").lower()
    if vendor == "Apple":
        return True
    if vendor == "NVIDIA":
        return False
    if vendor == "Intel":
        if any(marker in lowered for marker in (" arc a", " arc b", "arc pro", "data center gpu flex")):
            return False
        if any(marker in lowered for marker in ("uhd graphics", "iris", "hd graphics")):
            return True
        return None
    if vendor == "AMD":
        if any(marker in lowered for marker in ("radeon graphics", "780m", "760m", "740m", "680m", "660m", "890m")):
            return True
        if any(marker in lowered for marker in ("radeon rx", "radeon pro", "instinct")):
            return False
    return None


def accelerator_record(
    *, name: str, vendor: str = "unknown", memory_gb: float | None = None,
    memory_kind: str = "unknown", integrated: bool | None = None, probe: str,
    device_id: str = "",
    presence_verified: bool | None = True,
) -> dict:
    """Build the normalized record emitted by platform accelerator probes."""
    if integrated is None:
        integrated = looks_integrated(name, vendor)
    return {
        "name": str(name or "display adapter"),
        "vendor": vendor,
        "memory_gb": round(float(memory_gb), 1) if memory_gb else None,
        "memory_kind": memory_kind,
        "integrated": integrated if isinstance(integrated, bool) else None,
        "probe": probe,
        "device_id": str(device_id or ""),
        "presence_verified": (
            presence_verified if isinstance(presence_verified, bool) else None
        ),
        # Detection proves only that the OS enumerates a device. Ollama/backend
        # readiness requires a separate runtime probe and is intentionally not
        # inferred from a vendor name or installed display driver.
        "runtime_ready": None,
    }
