import pytest

from sonder_runtime.platform import child_environment_policy
from sonder_runtime.platform import logging as runtime_logging
from sonder_runtime.platform.config import SECRET_ENV_KEYS


def test_child_secret_policy_owns_classification():
    assert runtime_logging._unsafe_child_secret_name is child_environment_policy.unsafe_child_secret_name


def test_logging_secret_environment_list_covers_typed_config_secrets():
    assert set(SECRET_ENV_KEYS) <= set(runtime_logging.SECRET_ENV_VARS)


def test_child_secret_policy_rejects_control_and_secret_names():
    unsafe = ("SONDER_API_KEY", "DATABASE_URL", "BUILD_APPROVAL", "CUSTOM_TOKEN")
    assert all(child_environment_policy.unsafe_child_secret_name(name) for name in unsafe)


def test_child_secret_policy_allows_unrelated_environment_names():
    safe = ("PATH", "LANG", "PYTHONIOENCODING", "WORKSPACE_ROOT")
    assert not any(child_environment_policy.unsafe_child_secret_name(name) for name in safe)


@pytest.mark.parametrize(
    "secret_name",
    tuple(dict.fromkeys((*SECRET_ENV_KEYS, *runtime_logging.SECRET_ENV_VARS))),
)
def test_child_environment_strips_every_config_secret_name(secret_name):
    child = runtime_logging.child_environment(
        {"PATH": "safe-path", secret_name: "private-config-value"}
    )

    assert child == {"PATH": "safe-path"}


def test_child_environment_strips_generic_authority_names_outside_unsafe_lab():
    child = runtime_logging.child_environment({
        "PATH": "safe-path",
        "DATABASE_URL": "postgres://private",
        "CUSTOM_CONTROL_GATE": "authority",
        "SONDER_HOME": "private-runtime-home",
    })

    assert child == {"PATH": "safe-path"}
