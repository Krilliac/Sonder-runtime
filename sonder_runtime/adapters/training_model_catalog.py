"""Immutable per-lifecycle model catalog and durable transition identities.

The ContextVar owns a frozen catalog for one training/deployment critical
section. Nested helpers share it; concurrent threads/tasks do not share mutable
state. Recovery reads aliases from its journal, never the live operator overlay.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import sonder_runtime.adapters.model_lists as model_lists
from sonder_runtime.domain.runtime_policy.rules import _MODEL_RE, is_cloud_name, is_reserved_personal_alias

_ACTIVE: ContextVar[model_lists.ModelListsConfig | None] = ContextVar("training_model_catalog", default=None)


def current() -> model_lists.ModelListsConfig:
    pinned = _ACTIVE.get()
    return model_lists.load() if pinned is None else pinned


@contextmanager
def operation() -> Iterator[None]:
    """Pin one immutable snapshot until the lifecycle lock is released."""
    token = _ACTIVE.set(current())
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def record() -> dict[str, Any]:
    document = current().document()
    return {"model_catalog": {
        "version": 1,
        "personal_model": document["reserved_personal_model"],
        "rollback_model": document["stable_alias"],
        "training_model_specs": document["training_model_specs"],
    }}


def journal(payload: Mapping[str, Any]) -> dict[str, Any]:
    """New journals pin identity; subsequent phase writes preserve it exactly."""
    return {**record(), **payload} if "model_catalog" not in payload else dict(payload)


def aliases(payload: Mapping[str, Any]) -> tuple[str, str]:
    catalog = payload.get("model_catalog")
    if not isinstance(catalog, dict) or type(catalog.get("version")) is not int or catalog["version"] != 1:
        raise ValueError("journal has no supported pinned model catalog; operator recovery is required")
    names = []
    for key in ("personal_model", "rollback_model"):
        model = catalog.get(key)
        if not isinstance(model, str) or not _MODEL_RE.fullmatch(model) or is_cloud_name(model):
            raise ValueError("journal has an invalid pinned model alias")
        names.append(model)
    return names[0], names[1]



def owner_is_routed(owner: Mapping[str, Any], bindings: Mapping[str, object]) -> bool:
    """Judge the persisted owner's alias with the shared reserved-alias rule."""
    model = owner.get("model")
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model) or is_cloud_name(model):
        raise ValueError("personal-alias owner has an invalid recorded model alias")
    return any(is_reserved_personal_alias(binding, model) for binding in bindings.values())
