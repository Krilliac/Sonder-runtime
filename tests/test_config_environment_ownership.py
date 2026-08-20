from __future__ import annotations

from sonder_runtime.platform import config as packaged_config
from sonder_runtime.platform import config_environment


def test_scalar_environment_policy_owns_packaged_implementations():
    assert packaged_config._env_bool is config_environment.env_bool
    assert packaged_config._env_int is config_environment.env_int
    assert config_environment.env_bool.__module__ == config_environment.__name__
    assert config_environment.env_int.__module__ == config_environment.__name__


def test_env_bool_accepts_historical_truthy_spellings_and_rejects_others():
    assert all(config_environment.env_bool(value) for value in ("1", " true ", "YES", "on"))
    assert not config_environment.env_bool("false")
    assert not config_environment.env_bool("2")


def test_env_int_preserves_default_and_reports_malformed_values():
    errors: list[str] = []
    assert config_environment.env_int("N", {}, 7, errors) == 7
    assert config_environment.env_int("N", {"N": " 12 "}, 7, errors) == 12
    assert config_environment.env_int("N", {"N": "oops"}, 7, errors) == 7
    assert errors == ["N is not an integer"]
