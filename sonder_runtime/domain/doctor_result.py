"""Pure result normalization policy for doctor checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sonder_runtime.domain.doctor_status import coerce_status


def normalize_result(name: str, raw: Any) -> dict[str, str]:
    """Normalize a check result into a stable name/status/detail mapping.

    A mapping may provide its canonical name, while tuple and scalar results
    retain the registry name supplied by the doctor runner.
    """
    result_name = name
    detail = ""
    if isinstance(raw, Mapping):
        result_name = str(raw.get("name") or name)
        status = coerce_status(raw.get("status"))
        detail = "" if raw.get("detail") is None else str(raw.get("detail"))
    elif isinstance(raw, tuple) and len(raw) == 2:
        status = coerce_status(raw[0])
        detail = "" if raw[1] is None else str(raw[1])
    else:
        status = coerce_status(raw)
    return {"name": str(result_name), "status": status, "detail": detail}
