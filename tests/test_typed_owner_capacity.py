from __future__ import annotations

import sonder_runtime.adapters.web.lifecycle as lifecycle_module
from sonder_runtime.platform.config import ServerConfig, SonderConfig, CapacityConfig


def test_typed_owner_capacity_ignores_environment_override(monkeypatch):
    monkeypatch.setenv("SONDER_OWNER_MAX_INFLIGHT", "1")
    lifecycle_module.reset_for_tests()
    lifecycle_module.configure(
        SonderConfig(
            server=ServerConfig(owner_max_inflight=5),
            capacity=CapacityConfig(http_requests=4, queue_depth=8),
        )
    )

    instance = lifecycle_module.get()

    assert instance._owner_max_inflight == 5
    lifecycle_module.reset_for_tests()


def test_typed_owner_capacity_zero_uses_derived_value_not_environment(monkeypatch):
    monkeypatch.setenv("SONDER_OWNER_MAX_INFLIGHT", "1")
    lifecycle_module.reset_for_tests()
    lifecycle_module.configure(
        SonderConfig(
            server=ServerConfig(owner_max_inflight=0),
            capacity=CapacityConfig(http_requests=4, queue_depth=8),
        )
    )

    instance = lifecycle_module.get()

    assert instance._owner_max_inflight == 3
    lifecycle_module.reset_for_tests()
