"""I/O wiring for pure model-list consumers and legacy compatibility names."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Collection, Iterable, Mapping, MutableMapping, MutableSet
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


# Casefolded cloud defaults this process has bound, per cloud tier ("code",
# "general"): seeded by startup_configuration, grown by every resync.  Guarded
# by _APPLIED_LOCK; read and written only through the functions below.
_APPLIED_CLOUD_DEFAULTS: dict[str, MutableSet[str]] = {}
_APPLIED_LOCK = threading.Lock()


def _record_applied_cloud_defaults(defaults: Mapping[str, Any]) -> None:
    with _APPLIED_LOCK:
        _APPLIED_CLOUD_DEFAULTS.clear()
        for tier, model in defaults.items():
            if isinstance(model, str) and model:
                _APPLIED_CLOUD_DEFAULTS[tier] = {model.casefold()}


def refresh_live_cloud_tiers(
    tiers: MutableMapping[str, object], environment: Mapping[str, object], *,
    default_cloud_general_model: object,
) -> None:
    """Repair the legacy binding, then follow model-list cloud defaults live.

    Runs before every tier read, so a changed overlay ``default_cloud_models``
    or ``retired_cloud_models`` (from ``/runtime models set`` or the cloud
    model refresh) reaches the running process without a restart.  An
    unusable model-list file leaves the tiers as they are.
    """
    cloud_tier_policy.refresh_live_cloud_tiers(
        tiers,
        environment,
        default_cloud_general_model=default_cloud_general_model,
        legacy_cloud_general_model=values()["legacy_cloud_general_model"],
    )
    try:
        lists = values()
    except model_lists.ModelListsConfigurationError:
        return
    with _APPLIED_LOCK:
        if not _APPLIED_CLOUD_DEFAULTS:
            for tier, model in lists["default_cloud_models"].items():
                _APPLIED_CLOUD_DEFAULTS[tier] = {str(model).casefold()}
        moves = cloud_tier_policy.resync_cloud_tiers(
            tiers,
            environment,
            applied=_APPLIED_CLOUD_DEFAULTS,
            current_defaults=lists["default_cloud_models"],
            retired=lists["retired_cloud_models"],
            legacy_cloud_general_model=lists["legacy_cloud_general_model"],
        )
    for tier, before, after in moves:
        logging.getLogger(__name__).warning(
            "%s moved from %r to the current model-list default %r", tier, before, after
        )


def live_cloud_default(tier: str, fallback: str) -> str:
    """The current ``default_cloud_models[tier]`` unless it is retired; never raises."""
    try:
        lists = values()
        model = lists["default_cloud_models"][tier]
        retired = {str(name).casefold() for name in lists["retired_cloud_models"]}
    except (model_lists.ModelListsConfigurationError, KeyError, TypeError):
        return fallback
    if not isinstance(model, str) or not model or model.casefold() in retired:
        return fallback
    return model


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
        configuration = runtime_configuration(env, floor=floor)
        _record_applied_cloud_defaults(values()["default_cloud_models"])
        return configuration
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
