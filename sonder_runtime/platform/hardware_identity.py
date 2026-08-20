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
