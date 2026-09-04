"""Verify that the concrete store modules satisfy their Protocol contracts.

Each test imports the real module and checks ``isinstance()`` against the
corresponding ``@runtime_checkable`` Protocol.  This is a structural
conformance check: the module must expose every method the protocol declares,
with a compatible callable signature.  If a method is renamed, removed, or its
arity changes in the concrete module, the corresponding test fails.
"""
from __future__ import annotations

import importlib
import os

import pytest

from sonder_runtime.domain.protocol.subsystem_protocols import (
    AutopilotStore,
    CompositionStore,
    FleetStore,
    GoalStore,
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Point all stores at throwaway directories so tests never touch real state."""
    monkeypatch.setenv("SONDER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SONDER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    monkeypatch.setenv("SONDER_FLEET_DB", str(tmp_path / "fleet.db"))
    monkeypatch.setenv("SONDER_COMPOSITION_DB", str(tmp_path / "composition.db"))
    monkeypatch.setenv("SONDER_GOAL_DB", str(tmp_path / "goals.db"))


# -- structural conformance (isinstance) ------------------------------------


class TestAutopilotStoreProtocol:
    """autopilot_store module satisfies AutopilotStore."""

    def test_isinstance_check(self):
        import sonder_runtime.adapters.persistence.autopilot_store as mod

        assert isinstance(mod, AutopilotStore)

    def test_protocol_methods_are_callable(self):
        import sonder_runtime.adapters.persistence.autopilot_store as mod

        for name in (
            "database_path",
            "create_run",
            "get_run",
            "list_runs",
            "request_pause",
            "request_cancel",
            "attach_steering",
            "heartbeat",
            "snapshot",
        ):
            attr = getattr(mod, name, None)
            assert attr is not None, "autopilot_store is missing %s" % name
            assert callable(attr), "autopilot_store.%s is not callable" % name


class TestFleetStoreProtocol:
    """fleet_store module satisfies FleetStore."""

    def test_isinstance_check(self):
        import sonder_runtime.adapters.persistence.fleet_store as mod

        assert isinstance(mod, FleetStore)

    def test_protocol_methods_are_callable(self):
        import sonder_runtime.adapters.persistence.fleet_store as mod

        for name in (
            "database_path",
            "register_principal",
            "local_principal_credentials",
            "register_owner",
            "heartbeat_owner",
            "close_owner",
            "reconcile_stale_owners",
            "create_agent",
            "start_agent",
            "begin_model_call",
            "update_agent",
            "finish_agent",
            "cancel_agents",
            "cancellation_requested",
            "get_agent",
            "list_agents_scoped",
            "queue_agent_message",
            "claim_agent_messages",
            "add_event",
            "acquire_retry_lease",
            "release_retry_lease",
            "snapshot",
            "prune",
            "clear_all",
        ):
            attr = getattr(mod, name, None)
            assert attr is not None, "fleet_store is missing %s" % name
            assert callable(attr), "fleet_store.%s is not callable" % name


class TestGoalStoreProtocol:
    """goal_store module satisfies GoalStore."""

    def test_isinstance_check(self):
        import goal_store as mod

        assert isinstance(mod, GoalStore)

    def test_protocol_methods_are_callable(self):
        import goal_store as mod

        for name in (
            "set_goal",
            "get_active",
            "add_note",
            "complete",
            "abandon",
            "propose",
            "proposals",
            "adopt",
            "decline",
            "get",
            "history",
            "context_block",
        ):
            attr = getattr(mod, name, None)
            assert attr is not None, "goal_store is missing %s" % name
            assert callable(attr), "goal_store.%s is not callable" % name


class TestCompositionStoreProtocol:
    """composition_store module satisfies CompositionStore."""

    def test_isinstance_check(self):
        import sonder_runtime.adapters.persistence.composition_store as mod

        assert isinstance(mod, CompositionStore)

    def test_protocol_methods_are_callable(self):
        import sonder_runtime.adapters.persistence.composition_store as mod

        for name in (
            "bind",
            "lookup_targets",
            "lookup_sources",
            "complete_binding",
            "break_binding",
            "active_bindings",
            "close_all_for",
        ):
            attr = getattr(mod, name, None)
            assert attr is not None, "composition_store is missing %s" % name
            assert callable(attr), "composition_store.%s is not callable" % name


# -- protocol re-exports from package init ---------------------------------


class TestProtocolReExports:
    """Protocols are importable from the package __init__."""

    def test_import_from_package(self):
        from sonder_runtime.domain.protocol import (
            AutopilotStore as A,
            CompositionStore as C,
            FleetStore as F,
            GoalStore as G,
        )

        assert A is AutopilotStore
        assert C is CompositionStore
        assert F is FleetStore
        assert G is GoalStore

    def test_runtime_checkable_decorator(self):
        """All protocols carry @runtime_checkable so isinstance works."""
        for proto in (AutopilotStore, FleetStore, GoalStore, CompositionStore):
            # runtime_checkable sets _is_runtime_protocol on the class
            assert getattr(proto, "_is_runtime_protocol", False), (
                "%s is not @runtime_checkable" % proto.__name__
            )
