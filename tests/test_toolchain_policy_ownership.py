import environment_probe

from sonder_runtime.platform import environment_probe as packaged_environment_probe
from sonder_runtime.platform import toolchain_policy


def test_toolchain_policy_owns_fixed_argument_allowlist():
    assert toolchain_policy.allowed_arguments(" cargo ") == ("--version",)
    assert toolchain_policy.allowed_arguments("made-up") is None
    assert toolchain_policy.VERSION_ARGUMENTS["go"] == ("version",)


def test_toolchain_policy_resolves_only_canonical_discovery(monkeypatch):
    profile = {"toolchains": {"cargo": "C:\\tools\\cargo.exe"}, "specialist_tools": {}}
    monkeypatch.setattr(packaged_environment_probe, "probe", lambda refresh=False: profile)
    assert toolchain_policy.discovered_path(" CARGO ") == "C:\\tools\\cargo.exe"
    assert environment_probe.probe() is profile
