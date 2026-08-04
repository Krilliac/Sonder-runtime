"""Runtime-policy use cases (SPEC-3 Phase 2).

Thin, typed application services over the PolicyRepository port. The
policy can select local aliases, routing lanes, and bounded NPU behavior
— it can never widen network, filesystem, credential, or cloud
permissions, and nothing at this layer can change that.
"""
from __future__ import annotations

from dataclasses import dataclass

from ...domain.common.errors import Conflict, ConcurrencyConflict, InvalidInput
from ...domain.runtime_policy import rules
from ..context import OperationContext
from ..ports.repositories import PolicyRepository


@dataclass(frozen=True)
class PolicyView:
    revision: int
    local_models: dict
    routing: dict
    npu: dict
    source: str

    @classmethod
    def from_raw(cls, raw: dict) -> "PolicyView":
        normalized = rules.normalize(raw)
        return cls(
            revision=normalized["revision"],
            local_models=normalized["local_models"],
            routing=normalized["routing"],
            npu=normalized["npu"],
            source=normalized["source"],
        )


class RuntimePolicyService:
    def __init__(self, repository: PolicyRepository) -> None:
        self._repository = repository

    def get(self, context: OperationContext) -> PolicyView:
        del context  # read access is unprivileged for the local owner
        return PolicyView.from_raw(self._repository.load())

    def update(
        self,
        context: OperationContext,
        *,
        local_models: dict | None = None,
        routing: dict | None = None,
        npu: dict | None = None,
        expected_revision: int | None = None,
    ) -> PolicyView:
        if context.auth_level not in ("local", "developer", "admin"):
            raise InvalidInput("policy updates require developer authority")
        try:
            raw = self._repository.update(
                local_models=local_models,
                routing=routing,
                npu=npu,
                expected_revision=expected_revision,
                source=f"{context.source}:{context.principal_id}",
            )
        except ValueError as exc:
            raise InvalidInput(str(exc)) from exc
        except RuntimeError as exc:
            # The legacy repository signals optimistic-concurrency loss and
            # deployment blocks as RuntimeError; map to the taxonomy.
            message = str(exc)
            if "concurrently" in message or "revision" in message:
                raise ConcurrencyConflict(message) from exc
            raise Conflict(message) from exc
        return PolicyView.from_raw(raw)

    def npu_mode(self, context: OperationContext, capability: str) -> str:
        del context
        return rules.npu_mode(capability, self._repository.load())
