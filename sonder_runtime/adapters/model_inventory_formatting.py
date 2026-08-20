"""Pure presentation helpers for the live Ollama model inventory."""
from __future__ import annotations

import math


def inventory_model_name(row) -> str:
    """Return the stable display name from an Ollama inventory row."""
    return str(row.get("name") or row.get("model") or "").strip()


def inventory_model_names(rows) -> list:
    """Return casefold-deduplicated inventory names in display order."""
    names, seen = [], set()
    for row in rows:
        name = inventory_model_name(row)
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    names.sort(key=str.casefold)
    return names


def residency_display(row) -> str:
    """Render one resident model with bounded VRAM residency indicators."""
    name = inventory_model_name(row)
    if not name:
        return ""

    def byte_count(value, minimum):
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            # Do not coerce arbitrary JSON integers to float: a malformed
            # provider can send a value too large for IEEE-754.
            return minimum <= value <= (2**63 - 1)
        return (
            isinstance(value, float)
            and math.isfinite(value)
            and value >= minimum
        )

    size = row.get("size")
    vram = row.get("size_vram")
    if not byte_count(size, 1) or not byte_count(vram, 0):
        return name
    # A provider claiming more VRAM than the model's own size is nonsense;
    # clamp rather than advertising >100% GPU.
    vram = min(float(vram), float(size))
    gib = float(size) / float(2**30)
    if vram == 0:
        return "%s (%.1f GiB, CPU only)" % (name, gib)
    return "%s (%.1f GiB, %d%% GPU)" % (
        name, gib, int(round(100.0 * vram / float(size))),
    )
