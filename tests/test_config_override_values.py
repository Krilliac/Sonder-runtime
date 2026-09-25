"""``--set`` overrides are explicit operator input and must be exact.

Live testing found ``--set backup.enabled=maybe`` silently became ``false``
(disabling backups) and every float key (``spanda.threshold``...) was
impossible to override because the raw string reached validation.
"""
from __future__ import annotations

import pytest

from sonder_runtime.platform import config as sonder_config


def _load(**overrides):
    return sonder_config.load_config(None, env={}, overrides=overrides)


@pytest.mark.parametrize("raw", ["maybe", "2", "", "enabled", "nope"])
def test_invalid_boolean_override_is_rejected(raw):
    with pytest.raises(sonder_config.ConfigError) as caught:
        _load(**{"backup.enabled": raw})
    assert "override 'backup.enabled' has invalid value" in str(caught.value)


@pytest.mark.parametrize(
    "raw, expected",
    [("true", True), ("1", True), ("YES", True), ("on", True),
     ("false", False), ("0", False), ("No", False), ("off", False)],
)
def test_boolean_override_spellings(raw, expected):
    assert _load(**{"backup.enabled": raw}).backup.enabled is expected


def test_float_override_is_applied():
    config = _load(**{"spanda.threshold": "0.5", "spanda.alpha": "0.25"})
    assert config.spanda.threshold == 0.5
    assert config.spanda.alpha == 0.25


@pytest.mark.parametrize("raw", ["abc", "nan", "inf", "-inf"])
def test_non_finite_or_malformed_float_override_is_rejected(raw):
    with pytest.raises(sonder_config.ConfigError) as caught:
        _load(**{"spanda.threshold": raw})
    assert "spanda.threshold" in str(caught.value)


def test_environment_boolean_compatibility_is_unchanged():
    # The historical environment spellings stay lenient; only --set is strict.
    config = sonder_config.load_config(None, env={"SONDER_WEB_TOOLS": "maybe"})
    assert config.features.web is False
