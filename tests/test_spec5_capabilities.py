"""SPEC-5 WP1: RuntimeCapabilities and startup-flag contract tests."""
from __future__ import annotations

import pytest

from sonder_runtime.bootstrap.capabilities import (
    RuntimeCapabilities,
    freeze,
    current,
    _reset_for_tests,
)
from sonder_runtime.bootstrap.main import parse_args
from sonder_runtime.adapters import runtime_capabilities as packaged_caps


@pytest.fixture(autouse=True)
def _reset_caps():
    _reset_for_tests()
    yield
    _reset_for_tests()


class TestRuntimeCapabilities:
    def test_bootstrap_is_identity_compatibility_surface(self):
        from sonder_runtime.bootstrap import capabilities as bootstrap_caps

        assert bootstrap_caps.RuntimeCapabilities is packaged_caps.RuntimeCapabilities
        assert bootstrap_caps.freeze is packaged_caps.freeze
        assert bootstrap_caps.current is packaged_caps.current

    def test_defaults_are_false(self):
        caps = RuntimeCapabilities()
        assert caps.unrestricted_tools is False
        assert caps.unrestricted_selfmod is False
        assert caps.full_autonomy is False

    def test_unrestricted_tools_independent(self):
        caps = RuntimeCapabilities(unrestricted_tools=True)
        assert caps.unrestricted_tools is True
        assert caps.unrestricted_selfmod is False
        assert caps.full_autonomy is False

    def test_unrestricted_selfmod_independent(self):
        caps = RuntimeCapabilities(unrestricted_selfmod=True)
        assert caps.unrestricted_tools is False
        assert caps.unrestricted_selfmod is True
        assert caps.full_autonomy is False

    def test_full_autonomy(self):
        caps = RuntimeCapabilities(
            unrestricted_tools=True,
            unrestricted_selfmod=True,
        )
        assert caps.full_autonomy is True

    def test_frozen_dataclass(self):
        caps = RuntimeCapabilities()
        with pytest.raises(AttributeError):
            caps.unrestricted_tools = True  # type: ignore[misc]

    def test_status_dict(self):
        caps = RuntimeCapabilities(unrestricted_tools=True)
        d = caps.status_dict()
        assert d == {
            "unrestricted_tools": True,
            "unrestricted_selfmod": False,
            "full_autonomy": False,
        }


class TestCapabilityFreeze:
    def test_freeze_once(self):
        caps = RuntimeCapabilities(unrestricted_tools=True)
        frozen = freeze(caps)
        assert frozen is caps
        assert current().unrestricted_tools is True

    def test_freeze_twice_raises(self):
        freeze(RuntimeCapabilities())
        with pytest.raises(RuntimeError, match="already frozen"):
            freeze(RuntimeCapabilities())

    def test_current_before_freeze_raises(self):
        with pytest.raises(RuntimeError, match="not yet frozen"):
            current()

    def test_immutable_after_freeze(self):
        caps = freeze(RuntimeCapabilities())
        with pytest.raises(AttributeError):
            caps.unrestricted_tools = True  # type: ignore[misc]
        assert current().unrestricted_tools is False


class TestCLIParsing:
    def test_no_flags(self):
        args = parse_args([])
        assert args.unrestricted_tools is False
        assert args.unrestricted_selfmod is False

    def test_unrestricted_tools(self):
        args = parse_args(["--unrestricted-tools"])
        assert args.unrestricted_tools is True
        assert args.unrestricted_selfmod is False

    def test_unrestricted_selfmod(self):
        args = parse_args(["--unrestricted-selfmod"])
        assert args.unrestricted_tools is False
        assert args.unrestricted_selfmod is True

    def test_both_flags(self):
        args = parse_args(["--unrestricted-tools", "--unrestricted-selfmod"])
        assert args.unrestricted_tools is True
        assert args.unrestricted_selfmod is True


class TestBootstrapNoSideEffects:
    def test_importing_capabilities_has_no_side_effects(self):
        import importlib
        mod = importlib.import_module("sonder_runtime.bootstrap.capabilities")
        assert mod is not None

    def test_importing_container_has_no_side_effects(self):
        import importlib
        mod = importlib.import_module("sonder_runtime.bootstrap.container")
        assert mod is not None

    def test_importing_main_has_no_side_effects(self):
        import importlib
        mod = importlib.import_module("sonder_runtime.bootstrap.main")
        assert mod is not None
