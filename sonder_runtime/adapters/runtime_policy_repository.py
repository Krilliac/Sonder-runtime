"""Canonical repository adapter for the runtime-policy application port.

The repository owns the boundary between the application policy use cases and
the runtime-policy persistence adapter.  Keeping it in a named module avoids
making the generic strangler collection the apparent owner of policy state.
"""
from __future__ import annotations


class RuntimePolicyRepository:
    """Implement ``PolicyRepository`` over the runtime-policy adapter."""

    def load(self) -> dict:
        import sonder_runtime.adapters.runtime_policy as runtime_policy

        return runtime_policy.load()

    def update(
        self,
        *,
        local_models: dict | None = None,
        routing: dict | None = None,
        npu: dict | None = None,
        expected_revision: int | None = None,
        source: str = "application",
    ) -> dict:
        import sonder_runtime.adapters.runtime_policy as runtime_policy

        return runtime_policy.update(
            local_models=local_models,
            routing=routing,
            npu=npu,
            source=source,
            expected_revision=expected_revision,
        )
