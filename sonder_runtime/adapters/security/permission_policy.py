"""Compatibility provider for the existing permission policy engine.

The root module remains the sole owner of state, persistence, and precedence.
This provider deliberately resolves it per call so test/runtime patches remain
visible while callers migrate to the port.
"""
from __future__ import annotations

import importlib


def _engine():
    return importlib.import_module("permission_modes")


class PermissionPolicyProvider:
    def __getattr__(self, name):
        return getattr(_engine(), name)

    def decide_for_caller(self, tool_name: str, *, interactive: bool,
                          gate_control_exempt: bool):
        return _engine().decide_for_caller(
            tool_name,
            interactive=interactive,
            gate_control_exempt=gate_control_exempt,
        )

    def set_mode(self, name: str) -> str:
        return _engine().set_mode(name)

    def modes(self) -> tuple[str, ...]:
        return tuple(_engine().MODES)

    def is_durable_authority_tool(self, name: str) -> bool:
        return str(name or "").lstrip("/") in _engine().DURABLE_AUTHORITY_TOOLS

    def mode_label(self, mode: str) -> str:
        return _engine().MODE_LABELS.get(mode, mode)


permission_policy = PermissionPolicyProvider()

__all__ = ["PermissionPolicyProvider", "permission_policy"]
