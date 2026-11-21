"""Shipped model catalogs and optional operator overlay; no Python defaults.

Invalid overlays are ignored as a whole and never rewritten. Missing or invalid
shipped data raises ModelListsConfigurationError at use time, never at import.
Dictionary overlays merge by subkey; lists and scalars replace their whole key.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from sonder_runtime.domain.routing.capability_router import TASK_CLASSES
from sonder_runtime.domain.runtime_policy.rules import (
    BASE_LOCAL_TIERS,
    LOCAL_TIERS,
    OPTIONAL_LOCAL_TIERS,
    ROUTING_LANES,
    _MODEL_RE,
    is_cloud_name,
)
import sonder_runtime.domain.runtime_policy.rules as policy_rules
import sonder_runtime.adapters.model_tool_policy as model_tool_policy
from sonder_runtime.platform import paths as runtime_paths

SCHEMA_VERSION = 1
OVERLAY_ENV = "SONDER_MODEL_LISTS"
OVERLAY_FILE_NAME = "model_lists.json"
SHIPPED_PATH = Path(__file__).resolve().parent / "data" / OVERLAY_FILE_NAME
MAX_FILE_BYTES = 1024 * 1024
FAILED_READ_TTL_SECONDS = 1.0
KEYS = frozenset(
    {
        "retired_cloud_models",
        "legacy_cloud_general_model",
        "default_cloud_models",
        "stable_alias",
        "default_local_models",
        "default_embedding_model",
        "reserved_personal_model",
        "default_routing",
        "vision_only_model_families",
        "tier_strength",
        "working_strength",
        "capability_router",
        "ensemble_skip_tiers",
        "bootstrap_models",
        "training_model_specs",
    }
)


class ConfigSource(str, Enum):
    SHIPPED = "shipped"
    OVERLAY = "overlay"
    ENV = "env"


@dataclass(frozen=True)
class ConfigIssue:
    """Immutable issue value; owned strings, no thread affinity."""

    source: ConfigSource
    path: str
    message: str

    def text(self) -> str:
        return "%s config %s: %s" % (self.source.value, self.path, self.message)


class ModelListsConfigurationError(ValueError):
    """The authoritative shipped catalog cannot be used; no fallback exists."""

    def __init__(self, path: str | os.PathLike[str], message: str) -> None:
        self.issue = ConfigIssue(ConfigSource.SHIPPED, str(path), str(message))
        super().__init__(self.issue.text())


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ModelListsConfig:
    """Owned immutable snapshot, safe for readers; document() returns a copy."""

    values: Mapping[str, Any]
    sources: Mapping[str, str]
    shipped_path: str
    overlay_path: str
    issues: tuple[ConfigIssue, ...] = ()

    def document(self) -> dict[str, Any]:
        return cast(dict[str, Any], _thaw(self.values))

    def source_of(self, key: str) -> str:
        return self.sources[key]


def overlay_path() -> Path:
    override = os.environ.get(OVERLAY_ENV, "").strip()
    return (
        Path(override).expanduser()
        if override
        else Path(runtime_paths.state_path(OVERLAY_FILE_NAME))
    )


def read_document(path: Path) -> Any:
    with Path(path).open("rb") as stream:
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("file exceeds %d bytes" % MAX_FILE_BYTES)
    return json.loads(content.decode("utf-8"))


def _object(value: Any, keys: Iterable[str], where: str) -> None:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(
            "%s must be an object with keys: %s" % (where, ", ".join(keys))
        )


def _model(value: Any, where: str, *, local: bool = False, unset: bool = False) -> None:
    if unset and value == "":
        return
    if not isinstance(value, str) or not _MODEL_RE.fullmatch(value):
        raise ValueError("%s must be a valid model name" % where)
    if local and is_cloud_name(value):
        raise ValueError("%s must name a local model" % where)


def _strings(value: Any, where: str, validate: Callable[[Any, str], None]) -> None:
    if not isinstance(value, list):
        raise ValueError("%s must be a list" % where)
    for item in value:
        validate(item, where)


def _tier(value: Any, where: str, allowed: Iterable[str] = LOCAL_TIERS) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("%s must use known tiers: %s" % (where, ", ".join(allowed)))


def _strength(value: Any, where: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("%s must be a non-negative integer" % where)


def validate(document: Any) -> None:
    """Validate a complete merged document, including its pinned HF revisions."""
    _object(document, ("version", *sorted(KEYS)), "model lists")
    if type(document["version"]) is not int or document["version"] != SCHEMA_VERSION:
        raise ValueError("version must be %d" % SCHEMA_VERSION)
    for key in ("stable_alias", "default_embedding_model", "reserved_personal_model"):
        _model(document[key], key, local=True)
    _model(document["legacy_cloud_general_model"], "legacy_cloud_general_model")
    for key in ("retired_cloud_models", "vision_only_model_families"):
        _strings(document[key], key, _model)
    for key, keys in (
        ("default_cloud_models", ("code", "general")),
        ("bootstrap_models", ("small", "medium", "large")),
        ("default_local_models", LOCAL_TIERS),
    ):
        _object(document[key], keys, key)
        for name, value in document[key].items():
            _model(
                value,
                key + "." + name,
                local=key != "default_cloud_models",
                unset=key == "default_local_models" and name in OPTIONAL_LOCAL_TIERS,
            )
    _object(document["default_routing"], ROUTING_LANES, "default_routing")
    for lane, tier in document["default_routing"].items():
        _tier(tier, "default_routing." + lane, BASE_LOCAL_TIERS)
    _object(document["tier_strength"], LOCAL_TIERS, "tier_strength")
    for tier, value in document["tier_strength"].items():
        _strength(value, "tier_strength." + tier)
    _strength(document["working_strength"], "working_strength")
    _strings(document["ensemble_skip_tiers"], "ensemble_skip_tiers", _tier)
    router = document["capability_router"]
    _object(
        router, ("preferred", "ladder", "fallback", "keywords"), "capability_router"
    )
    for key in ("preferred", "ladder"):
        _object(router[key], TASK_CLASSES, "capability_router." + key)
        for task, value in router[key].items():
            where = "capability_router.%s.%s" % (key, task)
            if key == "preferred":
                _tier(value, where)
            else:
                _strings(value, where, _tier)
                if not value:
                    raise ValueError(where + " must not be empty")
    _strings(
        router["fallback"],
        "capability_router.fallback",
        lambda v, w: _tier(v, w, BASE_LOCAL_TIERS),
    )
    if not router["fallback"]:
        raise ValueError("capability_router.fallback must not be empty")
    _object(
        router["keywords"],
        tuple(task for task in TASK_CLASSES if task != "simple"),
        "capability_router.keywords",
    )
    for task, words in router["keywords"].items():
        if not isinstance(words, list) or not all(
            isinstance(w, str) and w.strip() for w in words
        ):
            raise ValueError(
                "capability_router.keywords.%s must be a list of nonempty strings"
                % task
            )
    specs = document["training_model_specs"]
    # Size tokens and the planning algorithm remain code contracts, not models.
    _object(specs, ("1.5b", "3b", "7b"), "training_model_specs")
    for size, spec in specs.items():
        where = "training_model_specs." + size
        _object(
            spec,
            (
                "params",
                "hf",
                "hf_revision",
                "ollama",
                "train_vram",
                "train_ram",
                "infer_vram",
                "infer_ram",
            ),
            where,
        )
        _model(spec["hf"], where + ".hf")
        _model(spec["ollama"], where + ".ollama", local=True)
        if not isinstance(spec["hf_revision"], str) or not re.fullmatch(
            r"[0-9a-f]{40}", spec["hf_revision"]
        ):
            raise ValueError(
                where + ".hf_revision must be a pinned 40-character commit"
            )
        for key in ("params", "train_vram", "train_ram", "infer_vram", "infer_ram"):
            value = spec[key]
            if (
                type(value) not in (int, float)
                or not 0 < value <= 1e300
                or not math.isfinite(value)
            ):
                raise ValueError(where + "." + key + " must be finite and positive")


def merge(shipped: dict[str, Any], overlay: Any) -> dict[str, Any]:
    """Validate before returning; unknown keys are rejected at every depth."""
    if not isinstance(overlay, dict):
        raise ValueError("overlay must be a JSON object")
    if type(overlay.get("version")) is not int or overlay["version"] != SCHEMA_VERSION:
        raise ValueError("overlay version must be %d" % SCHEMA_VERSION)

    def apply(base: Any, patch: Any) -> Any:
        if isinstance(base, dict) and isinstance(patch, dict):
            unknown = set(patch) - set(base)
            if unknown:
                raise ValueError("unknown key(s): " + ", ".join(sorted(unknown)))
            result = copy.deepcopy(base)
            for key, value in patch.items():
                result[key] = apply(base[key], value)
            return result
        return copy.deepcopy(patch)

    result = cast(dict[str, Any], apply(shipped, overlay))
    validate(result)
    return result


def load_shipped_document() -> dict[str, Any]:
    try:
        document = read_document(SHIPPED_PATH)
        validate(document)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise ModelListsConfigurationError(SHIPPED_PATH, str(exc)) from exc
    return cast(dict[str, Any], document)


def _overlay_patch(path: Path) -> Any:
    """The overlay document at ``path``; a missing file is the empty overlay."""
    try:
        return read_document(path)
    except FileNotFoundError:
        return {"version": SCHEMA_VERSION}


def validate_overlay_file(path: Path) -> None:
    """Load ``path`` back through the loader's own overlay path; raise if it would be ignored."""
    merge(load_shipped_document(), _overlay_patch(Path(path)))


FileIdentity = tuple[str, int, int, int] | tuple[str, None]
IdentityKey = tuple[FileIdentity, FileIdentity]


def _identity(path: Path) -> FileIdentity | None:
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except FileNotFoundError:
        return (str(path), None)
    except OSError:
        return None


def identity_key() -> IdentityKey | None:
    try:
        shipped, overlay = _identity(SHIPPED_PATH), _identity(overlay_path())
        return (
            (shipped, overlay) if shipped is not None and overlay is not None else None
        )
    except (OSError, ValueError, TypeError):
        return None


_CACHE: tuple[IdentityKey, ModelListsConfig, float | None] | None = None


def clear_cache() -> None:
    global _CACHE
    _CACHE = None


def load() -> ModelListsConfig:
    global _CACHE
    key = identity_key()
    cached = _CACHE
    if (
        key is not None
        and cached is not None
        and cached[0] == key
        and (cached[2] is None or time.monotonic() < cached[2])
    ):
        return cached[1]
    shipped = load_shipped_document()
    effective = shipped
    patch: Any = {}
    issues: tuple[ConfigIssue, ...] = ()
    path = "(unknown)"
    try:
        path = str(overlay_path())
        patch = _overlay_patch(Path(path))
        effective = merge(shipped, patch)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        issues = (ConfigIssue(ConfigSource.OVERLAY, path, str(exc)),)
        patch = {}
    sources: dict[str, str] = {}

    def source_tree(
        value: Mapping[str, Any], overlay: Mapping[str, Any], prefix: str = ""
    ) -> None:
        for name, item in value.items():
            dotted = prefix + name
            sources[dotted] = "overlay" if name in overlay else "shipped"
            if isinstance(item, dict):
                sub = overlay.get(name, {})
                source_tree(item, sub if isinstance(sub, dict) else {}, dotted + ".")

    source_tree(effective, patch)
    config = ModelListsConfig(
        _freeze(effective), MappingProxyType(sources), str(SHIPPED_PATH), path, issues
    )
    if key is not None and identity_key() == key:
        _CACHE = (
            key,
            config,
            time.monotonic() + FAILED_READ_TTL_SECONDS if issues else None,
        )
    return config


def effective(
    config: ModelListsConfig, env: Mapping[str, str], *, issues: list[ConfigIssue] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """The defaults with existing environment overrides, for operator status."""
    data, sources = config.document(), dict(config.sources)
    floor = model_tool_policy.shipped_floor()
    for tier in LOCAL_TIERS:
        variable = "SONDER_" + tier.upper()
        try:
            configured = policy_rules.seed_model(
                env, tier, floor=floor, lists=config.values
            )
        except ValueError as exc:
            configured = config.values["default_local_models"][tier]
            if issues is not None:
                issues.append(ConfigIssue(ConfigSource.ENV, variable, str(exc)))
        data["default_local_models"][tier] = configured
        if variable in env and (
            configured != config.values["default_local_models"][tier]
            or str(env[variable]).strip() == configured
        ):
            sources["default_local_models." + tier] = "env"
        if tier == "code" and policy_rules.is_cloud_name(str(env.get(variable, ""))):
            if str(env.get("SONDER_CODE_LOCAL", "")).strip() == configured:
                sources["default_local_models.code"] = "env"
    try:
        embedding = policy_rules.seed_embedding_model(env, lists=config.values)
    except ValueError as exc:
        embedding = config.values["default_embedding_model"]
        if issues is not None:
            issues.append(ConfigIssue(ConfigSource.ENV, "SONDER_EMBED_MODEL", str(exc)))
    data["default_embedding_model"] = embedding
    if str(env.get("SONDER_EMBED_MODEL", "")).strip() == embedding:
        sources["default_embedding_model"] = "env"
    retired = {name.casefold() for name in data["retired_cloud_models"]}
    for tier in ("code", "general"):
        variable = "SONDER_CLOUD_" + tier.upper()
        value = str(env.get(variable, "") or "")
        if value.strip() and value.strip().casefold() not in retired:
            data["default_cloud_models"][tier] = value
            sources["default_cloud_models." + tier] = "env"
    return data, sources


def describe(config: ModelListsConfig | None = None) -> list[str]:
    try:
        config = load() if config is None else config
    except ModelListsConfigurationError as exc:
        return ["model lists unavailable: ERROR: " + str(exc)]
    lines = [
        "model lists (shipped): " + config.shipped_path,
        "model lists (overlay): " + config.overlay_path,
    ]
    lines.extend("ERROR: " + issue.text() for issue in config.issues)
    env_issues: list[ConfigIssue] = []
    data, sources = effective(config, os.environ, issues=env_issues)
    lines.extend("ERROR: " + issue.text() for issue in env_issues)

    def show(value: Mapping[str, Any], prefix: str = "") -> None:
        for key, item in value.items():
            dotted = prefix + key
            if isinstance(item, Mapping):
                show(item, dotted + ".")
            else:
                lines.append(
                    "%s = %s [%s]" % (dotted, json.dumps(_thaw(item)), sources[dotted])
                )

    show(data)
    # Environment is reported separately: it overrides defaults, not the file.
    for key in (
        "FAST",
        "CODE",
        "GENERAL",
        "REASONING",
        "VISION",
        "CLOUD_CODE",
        "CLOUD_GENERAL",
        "EMBED_MODEL",
        "CODE_LOCAL",
        "LEARN_TIERS",
    ):
        variable = "SONDER_" + key
        if variable in os.environ:
            lines.append(
                "%s = %s [env; overrides config defaults]"
                % (variable, json.dumps(os.environ[variable]))
            )
    return lines
