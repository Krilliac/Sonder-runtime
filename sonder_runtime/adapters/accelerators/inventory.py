"""Normalized accelerator-inventory policies."""

from __future__ import annotations


def dedupe_accelerators(records: list[dict]) -> list[dict]:
    """Drop exact/stale duplicates while retaining distinct physical adapters."""
    result = []
    seen = set()
    for item in records:
        device_id = str(item.get("device_id") or "").lower()
        key = (str(item.get("probe") or ""), device_id) if device_id else (
            str(item.get("vendor") or "unknown").lower(),
            str(item.get("name") or "display adapter").lower(),
            item.get("memory_gb"),
            item.get("integrated"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


__all__ = ["dedupe_accelerators"]
