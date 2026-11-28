"""Parse Sonder Inference ``/v1/models`` (and single-model dict) context/parameter/fitness metadata."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from sonder_runtime.domain import kv_budget as _kv_budget
from sonder_runtime.platform import context_policy, declared_context


def _parse_strict_int(value: object) -> int | None:
    parser = cast(Callable[[object], int | None], context_policy.parse_strict)
    return parser(value)


def _select_model_row(details: object, key: str) -> dict[str, Any] | None:
    """Select the matching model row from a ``/v1/models`` envelope, or return a single-model dict."""
    if not isinstance(details, dict):
        return None
    if "data" in details or "models" in details:
        rows = details.get("data") or details.get("models") or ()
        if not isinstance(rows, (list, tuple)):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = str(row.get("id") or row.get("name") or row.get("model") or "").strip().casefold()
            if row_id == key:
                return row
        return None
    return details


def extract_si_context_length(sonder_meta: Mapping[str, Any]) -> int | None:
    """Extract context length from SI's ``sonder`` extension object.

    Real Sonder Inference ``/v1/models`` rows emit:
    - ``sonder.runtime.context.fitted_ctx`` (active runtime fitted context)
    - ``sonder.profile.context_length`` (profile context, rewritten to fitted_ctx when active)
    - ``sonder.runtime.context.configured_ctx`` / ``sonder.profile.configured_context_length``
    """
    raw_runtime = sonder_meta.get("runtime")
    if isinstance(raw_runtime, Mapping):
        raw_ctx = raw_runtime.get("context")
        if isinstance(raw_ctx, Mapping):
            fitted = _parse_strict_int(raw_ctx.get("fitted_ctx"))
            if fitted is not None:
                return fitted
            configured = _parse_strict_int(raw_ctx.get("configured_ctx"))
            if configured is not None:
                return configured
    raw_profile = sonder_meta.get("profile")
    if isinstance(raw_profile, Mapping):
        profile_ctx = _parse_strict_int(raw_profile.get("context_length"))
        if profile_ctx is not None:
            return profile_ctx
        configured_profile = _parse_strict_int(raw_profile.get("configured_context_length"))
        if configured_profile is not None:
            return configured_profile
    return None


def parse_model_metadata(
    details: object, key: str,
) -> tuple[int | None, object, object, int | None]:
    """Extract ``(context_length, parameter_size, geometry, declared)`` from a ``/v1/models`` or model dict."""
    if not isinstance(details, dict):
        raise ValueError("invalid /v1/models metadata response")
    matched = _select_model_row(details, key)
    if matched is None:
        raise LookupError(key)
    raw_sonder = matched.get("sonder")
    sonder_meta: dict[str, Any] = raw_sonder if isinstance(raw_sonder, dict) else {}
    raw_profile = sonder_meta.get("profile")
    profile_meta: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    raw_info = matched.get("model_info")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    context_length = extract_si_context_length(sonder_meta)
    if context_length is None:
        for name, value in info.items():
            if str(name).casefold().endswith("context_length"):
                context_length = _parse_strict_int(value)
                if context_length:
                    break
    raw_detail = matched.get("details")
    detail_block: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
    parameter_size: object = profile_meta.get("parameter_size")
    if parameter_size is None:
        parameter_size = detail_block.get("parameter_size")
    if parameter_size is None:
        count = info.get("general.parameter_count")
        if count:
            parameter_size = float(count) / 1_000_000_000.0
    geometry = _kv_budget.geometry_from_model_info(info)
    declared = (
        extract_si_context_length(sonder_meta)
        or declared_context.declared_from_show(matched)
    )
    return context_length, parameter_size, geometry, declared


def extract_chat_fitness_payload(details: object, model: str) -> dict[str, Any]:
    """Select one model's row from ``/v1/models`` and normalize SI fields for ``chat_model_fitness``.

    Note: real Sonder Inference ``sonder.profile.capabilities`` only carries
    profile-level flags (such as ``["speculative"]`` or ``[]``), not chat vs.
    embedding capabilities, so it is intentionally NOT mapped onto fitness
    ``capabilities``.
    """
    key = str(model or "").strip().casefold()
    matched = _select_model_row(details, key) if key else None
    if matched is None:
        return {}
    out: dict[str, Any] = dict(matched)
    raw_sonder = matched.get("sonder")
    sonder_meta: dict[str, Any] = raw_sonder if isinstance(raw_sonder, dict) else {}
    raw_profile = sonder_meta.get("profile")
    profile_meta: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}

    si_ctx = extract_si_context_length(sonder_meta)
    if si_ctx is not None:
        raw_info = out.get("model_info")
        info: dict[str, Any] = dict(raw_info) if isinstance(raw_info, dict) else {}
        if not any(str(name).casefold().endswith("context_length") for name in info):
            info["sonder.context_length"] = si_ctx
        out["model_info"] = info

    if "details" not in out:
        family = profile_meta.get("family")
        families = profile_meta.get("families")
        if family is not None or families is not None:
            detail_map: dict[str, Any] = {}
            if family is not None:
                detail_map["family"] = family
            if families is not None:
                detail_map["families"] = families
            out["details"] = detail_map

    return out
