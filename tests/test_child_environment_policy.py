from sonder_runtime.platform import child_environment_policy
from sonder_runtime.platform import logging as runtime_logging


def test_child_secret_policy_owns_classification():
    assert runtime_logging._unsafe_child_secret_name is child_environment_policy.unsafe_child_secret_name


def test_child_secret_policy_rejects_control_and_secret_names():
    unsafe = ("SONDER_API_KEY", "DATABASE_URL", "BUILD_APPROVAL", "CUSTOM_TOKEN")
    assert all(child_environment_policy.unsafe_child_secret_name(name) for name in unsafe)


def test_child_secret_policy_allows_unrelated_environment_names():
    safe = ("PATH", "LANG", "PYTHONIOENCODING", "WORKSPACE_ROOT")
    assert not any(child_environment_policy.unsafe_child_secret_name(name) for name in safe)
