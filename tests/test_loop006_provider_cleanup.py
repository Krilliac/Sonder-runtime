from __future__ import annotations

from dataclasses import dataclass

from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.jobs.durable_registry import (
    ProcessTreeCleanupReceipt,
    ProcessTreeCleanupRequest,
)
from sonder_runtime.application.loop.process_cleanup import (
    LocalExecutionProviderRegistry,
)
from sonder_runtime.application.ports.specialized_lifecycle import CleanupResult


@dataclass
class Provider:
    provider_id: str
    complete: bool = True

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str | float | None]] = []

    def cancel(self, *, reason: str = "cancellation requested") -> bool:
        self.calls.append(("cancel", reason))
        return True

    def cleanup(self, timeout: float | None = None) -> CleanupResult:
        self.calls.append(("cleanup", timeout))
        return CleanupResult(self.provider_id, self.complete, self.complete, "released")


class Supervisor:
    def __init__(self, *, complete: bool = True) -> None:
        self.complete = complete
        self.requests: list[ProcessTreeCleanupRequest] = []

    def cleanup(self, request: ProcessTreeCleanupRequest) -> ProcessTreeCleanupReceipt:
        self.requests.append(request)
        return ProcessTreeCleanupReceipt(
            request.job_id, True, 1, 1 if self.complete else 0, self.complete,
            "tree released" if self.complete else "child remains",
        )


def request_for(provider_id: str, reason: str) -> ProcessTreeCleanupRequest:
    return ProcessTreeCleanupRequest(provider_id, 41, 41, reason=reason)


def test_fanout_cancels_and_cleans_every_registered_provider() -> None:
    first = Provider("local-a")
    second = Provider("local-b")
    registry = LocalExecutionProviderRegistry()
    registry.register(first, process_request=lambda reason: request_for("local-a", reason))
    registry.register(second, process_request=lambda reason: request_for("local-b", reason))
    supervisor = Supervisor()

    receipts = registry.cancel_and_cleanup(supervisor, reason="operator stop", timeout=2.0)

    assert [receipt.provider_id for receipt in receipts] == ["local-a", "local-b"]
    assert all(receipt.complete for receipt in receipts)
    assert first.calls == [("cancel", "operator stop"), ("cleanup", 2.0)]
    assert second.calls == [("cancel", "operator stop"), ("cleanup", 2.0)]
    assert [request.job_id for request in supervisor.requests] == ["local-a", "local-b"]


def test_unsupported_process_provider_is_reported_incomplete() -> None:
    provider = Provider("local-unsupported")
    registry = LocalExecutionProviderRegistry()
    registry.register(provider)

    receipt = registry.cancel_and_cleanup(Supervisor(), timeout=0.0)[0]

    assert provider.calls == [("cancel", "cancellation requested"), ("cleanup", 0.0)]
    assert not receipt.complete
    assert not receipt.process_cleanup.requested
    assert "unsupported" in receipt.process_cleanup.detail


def test_incomplete_provider_or_process_cleanup_is_never_upgraded() -> None:
    provider = Provider("local-busy", complete=False)
    registry = LocalExecutionProviderRegistry()
    registry.register(provider, process_request=lambda reason: request_for("local-busy", reason))

    receipt = registry.cancel_and_cleanup(
        Supervisor(complete=False), reason="timeout", timeout=0.0
    )[0]

    assert not receipt.complete
    assert not receipt.provider_cleanup.quiescent
    assert not receipt.process_cleanup.complete


def test_bad_process_request_is_fail_closed_without_skipping_provider_cleanup() -> None:
    provider = Provider("local-invalid")
    registry = LocalExecutionProviderRegistry()
    registry.register(provider, process_request=lambda _reason: object())  # type: ignore[return-value]

    receipt = registry.cancel_and_cleanup(Supervisor())[0]

    assert provider.calls[0][0] == "cancel"
    assert provider.calls[1][0] == "cleanup"
    assert not receipt.complete
    assert "failed" in receipt.process_cleanup.detail
