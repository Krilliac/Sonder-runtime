"""Parse Sonder Inference ``/v1/models`` (and single-model dict) context/parameter metadata."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from sonder_runtime.domain import kv_budget as _kv_budget
from sonder_runtime.platform import context_policy, declared_context


def _parse_strict_int(value: object) -> int | None:
    parser = cast(Callable[[object], int | None], context_policy.parse_strict)
    return parser(value)


def parse_model_metadata(
    details: object, key: str,
) -> tuple[int | None, object, object, int | None]:
    """Extract ``(context_length, parameter_size, geometry, declared)`` from a ``/v1/models`` or model dict."""
    if not isinstance(details, dict):
        raise ValueError("invalid /v1/models metadata response")
    matched: dict[str, Any]
    if "data" in details or "models" in details:
        rows = details.get("data") or details.get("models") or ()
        found = next(
            (
                r for r in rows
                if isinstance(r, dict)
                and str(r.get("id") or r.get("name") or "").strip().casefold() == key
            ),
            None,
        )
        if not isinstance(found, dict):
            raise LookupError(key)
        matched = found
    else:
        matched = details
    raw_sonder = matched.get("sonder")
    sonder_meta: dict[str, Any] = raw_sonder if isinstance(raw_sonder, dict) else {}
    raw_info = matched.get("model_info")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    context_length = _parse_strict_int(sonder_meta.get("context_length"))
    if context_length is None:
        for name, value in info.items():
            if str(name).casefold().endswith("context_length"):
                context_length = _parse_strict_int(value)
                if context_length:
                    break
    raw_detail = matched.get("details")
    detail_block: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
    parameter_size: object = sonder_meta.get("parameter_size")
    if parameter_size is None:
        parameter_size = detail_block.get("parameter_size")
    if parameter_size is None:
        count = info.get("general.parameter_count")
        if count:
            parameter_size = float(count) / 1_000_000_000.0
    geometry = _kv_budget.geometry_from_model_info(info)
    declared = (
        _parse_strict_int(sonder_meta.get("num_ctx"))
        or declared_context.declared_from_show(matched)
    )
    return context_length, parameter_size, geometry, declared
