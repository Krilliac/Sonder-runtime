"""I/O wiring for pure model-list consumers and legacy compatibility names."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Collection, Iterable, Mapping, MutableMapping
from typing import Any, cast

import sonder_runtime.adapters.model_lists as model_lists
import sonder_runtime.adapters.training_model_catalog as training_model_catalog
import sonder_runtime.domain.cloud_model_policy as cloud_model_policy
import sonder_runtime.domain.cloud_tier_policy as cloud_tier_policy
import sonder_runtime.domain.fanout_policy as fanout_policy
import sonder_runtime.domain.runtime_model_binding as runtime_model_binding
from sonder_runtime.domain.runtime_model_configuration import RuntimeModelConfiguration
from sonder_runtime.domain.tool_lane_models import ToolLaneFloor


def values() -> Mapping[str, Any]:
    return model_lists.load().values


_TRAINING_KEYS = {
    "PERSONAL_MODEL": "reserved_personal_model",
    "ROLLBACK_MODEL": "stable_alias",
    "MODEL_SPECS": "training_model_specs",
}


def training_value(name: str) -> Any:
    """Read a legacy adaptive_training constant from the loaded model lists."""
    return training_model_catalog.current().values[_TRAINING_KEYS[name]]


def training_attr(name: str) -> Any:
    """Module ``__getattr__`` for the legacy adaptive_training constant names."""
    if name not in _TRAINING_KEYS:
        raise AttributeError(name)
    return training_value(name)


def runtime_configuration(env: Mapping[str, str], *, floor: ToolLaneFloor) -> RuntimeModelConfiguration:
    return RuntimeModelConfiguration.from_environment(env, floor=floor, lists=values())


def live_cloud_model(
    configured: object, default: str, retired_models: Collection[str] | None = None
) -> object:
    retired = (
        values()["retired_cloud_models"] if retired_models is None else retired_models
    )
    return cloud_model_policy.live_cloud_model(configured, default, retired)


def refresh_live_cloud_tiers(
    tiers: MutableMapping[str, object], environment: Mapping[str, object], *,
    default_cloud_general_model: object,
) -> None:
    cloud_tier_policy.refresh_live_cloud_tiers(
        tiers,
        environment,
        default_cloud_general_model=default_cloud_general_model,
        legacy_cloud_general_model=values()["legacy_cloud_general_model"],
    )


def nonchat_reason(record: object) -> str:
    return fanout_policy.nonchat_reason(
        record, vision_only_model_families=values()["vision_only_model_families"]
    )


def model_capability_error(tier: str, model: str, records: Iterable[tuple[str, object]]) -> str:
    return runtime_model_binding.model_capability_error(
        tier,
        model,
        records,
        vision_only_model_families=values()["vision_only_model_families"],
    )


def startup_configuration(env: Mapping[str, str], *, floor: ToolLaneFloor) -> RuntimeModelConfiguration:
    """An unavailable projection has no models; model operations still raise.

    Keep the legacy MCP module importable so status can name the broken file.
    Empty fields are an unavailable sentinel, never usable model defaults.
    """
    try:
        return runtime_configuration(env, floor=floor)
    except model_lists.ModelListsConfigurationError as exc:
        logging.getLogger(__name__).error("%s", exc)
        return RuntimeModelConfiguration("", "", "", "", "", frozenset(), (), ())


def startup_seed(
    bindings: Mapping[str, str], seed: Callable[[Mapping[str, str]], dict[str, str]]
) -> dict[str, str]:
    try:
        return seed(bindings)
    except model_lists.ModelListsConfigurationError:
        return {}


def startup_refresh(refresh: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    try:
        return refresh(create=True)
    except model_lists.ModelListsConfigurationError as exc:
        logging.getLogger(__name__).error("%s", exc)
        return {}


def startup_embedding_model() -> str:
    try:
        return os.environ.get("SONDER_EMBED_MODEL", cast(str, values()["default_embedding_model"]))
    except model_lists.ModelListsConfigurationError:
        return ""


def startup_ensemble_skip_tiers() -> tuple[str, ...]:
    try:
        return tuple(values()["ensemble_skip_tiers"])
    except model_lists.ModelListsConfigurationError:
        return ()


def policy_status(
    data: Callable[[], dict[str, Any]], refresh: Callable[..., object],
    render: Callable[[dict[str, Any]], str],
) -> str:
    """Validate model data before refreshing and rendering read-only status."""
    try:
        model_lists.load()
        refresh(create_policy=False)
        return render(data())
    except model_lists.ModelListsConfigurationError:
        return "\n".join(model_lists.describe())
