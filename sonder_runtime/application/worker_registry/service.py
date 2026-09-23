from __future__ import annotations

from typing import Any, Mapping

from sonder_runtime.application.ports.subagents import SubagentBudget, SubagentRequest
from sonder_runtime.application.ports.worker_registry import (
    WorkerLaunch, WorkerRecord, WorkerRegistry, WorkerStatus,
)


def _metadata(request: SubagentRequest) -> dict[str, str]:
    return dict(request.metadata)


class WorkerRegistryService:
    """Translate durable child requests into one persisted worker envelope."""

    def __init__(self, repository: WorkerRegistry) -> None:
        self._repository = repository

    @staticmethod
    def launch_for(request: SubagentRequest) -> WorkerLaunch:
        metadata = _metadata(request)
        budget = request.budget
        budgets = {name: getattr(budget, name) for name in (
            "max_children", "max_depth", "max_concurrency", "max_steps",
            "max_wall_seconds", "max_output_tokens",
        ) if getattr(budget, name) is not None}
        scope = tuple(filter(None, metadata.get("scope", metadata.get("workspace_read_roots", "")).split("|")))
        tools = tuple(filter(None, metadata.get("allowed_tools", "").split("|")))
        resume_key = metadata.get("resume_key", request.child_id or "")
        return WorkerLaunch(
            worker_id=request.child_id or resume_key,
            parent_id=request.parent_id,
            role=metadata.get("role", "worker"),
            model=metadata.get("model", metadata.get("tier", "unknown")),
            backend=metadata.get("backend", "durable-child"),
            effort=metadata.get("effort", "default"),
            scope=scope,
            allowed_tools=tools,
            budgets=budgets,
            retry_policy={"max_attempts": int(metadata.get("retry_max_attempts", "1"))},
            resume_key=resume_key,
            idempotency_key=metadata.get("idempotency_key", resume_key),
            prompt=request.prompt,
            owner_id=metadata.get("owner_id", ""),
            metadata=tuple(request.metadata),
        )

    def admit(self, request: SubagentRequest) -> WorkerRecord:
        return self._repository.admit(self.launch_for(request))

    def start(self, worker_id: str, *, expected_revision: int) -> WorkerRecord | None:
        return self._repository.start(worker_id, expected_revision=expected_revision)

    def get(self, worker_id: str) -> WorkerRecord | None:
        return self._repository.get(worker_id)

    def progress(self, worker_id: str, progress: Mapping[str, Any], *, expected_revision: int) -> WorkerRecord | None:
        return self._repository.progress(worker_id, progress, expected_revision=expected_revision)

    def finish(self, worker_id: str, *, status: WorkerStatus, verification: Mapping[str, Any], error: str = "", expected_revision: int) -> WorkerRecord | None:
        return self._repository.finish(worker_id, status=status, verification=verification, error=error, expected_revision=expected_revision)


__all__ = ["WorkerRegistryService"]
