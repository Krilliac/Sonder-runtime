"""SaaS feature switches and monthly token-tier limits.

SaaS mode is OFF unless an operator turns a switch on: every feature in
:data:`FEATURES` defaults to ``False``, and with ``usage_metering_enabled``
off nothing is metered.  The switches and any tier-limit overrides live in the
accounts database's ``saas_settings`` table (owned and read by
``admin_auth``); this module only parses those rows, so it holds no I/O, no
connection and no process state.

A :class:`SaaSConfig` is an immutable snapshot of the rows it was built from.
It is safe to share across threads; build a new one to see a later change.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

FEATURES: tuple[str, ...] = (
    "usage_metering_enabled",
    "enterprise_tier_enabled",
    "cloud_inference_enabled",
    "advanced_agents_enabled",
    "admin_ui_enabled",
    "observability_enabled",
)

# Monthly token allowance per account tier, used only while metering is on.
DEFAULT_TIER_LIMITS: Mapping[str, int] = MappingProxyType({
    "free": 50000,
    "pro": 250000,
    "pro+": 500000,
    "enterprise": 1000000,
})

FEATURE_PREFIX = "feature:"
TIER_LIMIT_PREFIX = "tier_limit:"
_TRUE = frozenset({"1", "true", "yes", "on"})
_MAX_TIER_NAME = 32


def feature_key(name: str) -> str:
    """The ``saas_settings`` key for feature ``name``; ``ValueError`` for an unknown one."""
    if name not in FEATURES:
        raise ValueError("unknown SaaS feature")
    return FEATURE_PREFIX + name


def tier_limit_key(tier: str, limit: int) -> str:
    """The ``saas_settings`` key for a tier's monthly limit; ``ValueError`` when unusable."""
    tier = str(tier or "").strip().lower()
    if not tier or len(tier) > _MAX_TIER_NAME or any(not 33 <= ord(c) <= 126 for c in tier):
        raise ValueError("invalid tier name")
    if type(limit) is not int or limit < 0:
        raise ValueError("tier limit must be a non-negative integer")
    return TIER_LIMIT_PREFIX + tier


def _all_off() -> Mapping[str, bool]:
    return MappingProxyType({name: False for name in FEATURES})


@dataclass(frozen=True)
class SaaSConfig:
    """Immutable switches and tier limits; build with :meth:`from_rows`."""

    features: Mapping[str, bool] = field(default_factory=_all_off)
    tier_limits: Mapping[str, int] = field(default_factory=lambda: DEFAULT_TIER_LIMITS)

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[str, str]]) -> "SaaSConfig":
        """Parse ``(key, value)`` rows; unknown keys and unparsable values are ignored."""
        features = {name: False for name in FEATURES}
        limits = dict(DEFAULT_TIER_LIMITS)
        for key, value in rows:
            key, text = str(key or ""), str(value or "").strip().lower()
            if key.startswith(FEATURE_PREFIX):
                name = key[len(FEATURE_PREFIX):]
                if name in features:
                    features[name] = text in _TRUE
            elif key.startswith(TIER_LIMIT_PREFIX):
                tier = key[len(TIER_LIMIT_PREFIX):]
                if tier and text.isascii() and text.isdigit():
                    limits[tier] = int(text)
        return cls(MappingProxyType(features), MappingProxyType(limits))

    def is_feature_enabled(self, feature: str) -> bool:
        return bool(self.features.get(feature, False))

    def get_tier_limit(self, tier: str) -> int:
        """The monthly token limit for ``tier``; an unknown tier gets the ``free`` limit."""
        return int(self.tier_limits.get(str(tier or "free"), self.tier_limits.get("free", 0)))


__all__ = ["DEFAULT_TIER_LIMITS", "FEATURES", "SaaSConfig", "feature_key", "tier_limit_key"]
