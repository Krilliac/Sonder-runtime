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
                          gate_control_exempt: bool, surface: str = "",
                          record: bool = True, mode: str | None = None,
                          rule_lookup=None, arguments=None, fence=None):
        return _engine().decide_for_caller(
            tool_name,
            interactive=interactive,
            gate_control_exempt=gate_control_exempt,
            surface=surface,
            record=record,
            mode=mode,
            rule_lookup=rule_lookup,
            arguments=arguments,
            fence=fence,
        )

    def call_digest(self, tool_name: str, arguments) -> str:
        return _engine().call_digest(tool_name, arguments)

    def approval_spent_for(self, tool_name: str, arguments) -> bool:
        return _engine().approval_spent_for(tool_name, arguments)

    def forget_spent_approval(self) -> None:
        _engine().forget_spent_approval()

    def approval_ledger(self):
        return _engine().approval_ledger()

    def unattended_summary(self) -> str:
        return _engine().unattended_summary()

    def unattended_refused_risks(self) -> frozenset[str]:
        return _engine().UNATTENDED_REFUSED_RISKS

    def set_mode(self, name: str) -> str:
        return _engine().set_mode(name)

    def modes(self) -> tuple[str, ...]:
        return tuple(_engine().MODES)

    def is_durable_authority_tool(self, name: str) -> bool:
        return str(name or "").lstrip("/") in _engine().DURABLE_AUTHORITY_TOOLS

    def mode_label(self, mode: str) -> str:
        return _engine().MODE_LABELS.get(mode, mode)

    # These are deliberately methods on the application-facing provider,
    # rather than constants imported by interfaces.  The root module remains
    # a compatibility engine, while production callers depend on the port's
    # vocabulary and can no longer reach into its module namespace.
    def gate_control_tools(self) -> frozenset[str]:
        return _engine().GATE_CONTROL_TOOLS

    def allow_action(self) -> str:
        return _engine().ALLOW

    def ask_action(self) -> str:
        return _engine().ASK

    def deny_action(self) -> str:
        return _engine().DENY


permission_policy = PermissionPolicyProvider()

__all__ = ["PermissionPolicyProvider", "permission_policy"]
