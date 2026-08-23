"""Offline, deterministic reliability scenarios across runtime boundaries."""
from __future__ import annotations

import io
from pathlib import Path
import socket
import sqlite3
import urllib.error

import pytest

from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.inference.injected import InjectedModelGateway
from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.persistence.sqlite.job_registry import (
    SQLiteDurableJobRegistry,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.execution.process_jobs import ProcessJobRequest
from sonder_runtime.application.jobs.durable_registry import (
    DurableJobRegistry,
    ProcessTreeCleanupReceipt,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.ports.model_target import ModelTarget
from sonder_runtime.domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    ConcurrencyConflict,
    DeadlineExceeded,
    DependencyUnavailable,
    IntegrityFailure,
)
from tests.fixtures.fault_injection import (
    Invoke,
    MutableCancellationToken,
    Raise,
    Return,
    SQLiteFaultConnector,
    ScriptedCall,
    ScriptedProcess,
)


pytestmark = pytest.mark.unit


def _identity(job_id: str) -> JobIdentity:
    return JobIdentity(job_id, "process", f"op-{job_id}", f"idem-{job_id}")


def _context(token: MutableCancellationToken):
    return local_owner_context(
        correlation_id="fault-injection",
        cancellation=token,
        timeout_seconds=30,
    )


def _cancel_then(token: MutableCancellationToken, value):
    def complete(*_args, **_kwargs):
        token.cancel()
        return value

    return complete


def _injected_race(token: MutableCancellationToken):
    call = ScriptedCall([Invoke(_cancel_then(token, "late answer"))])
    gateway = InjectedModelGateway(generate=call)
    return gateway, call


def _openai_race(token: MutableCancellationToken):
    payload = {"choices": [{"message": {"content": "late answer"}}]}
    call = ScriptedCall([Invoke(_cancel_then(token, payload))])
    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig("http://127.0.0.1:8080", model="local"),
        transport=call,
    )
    return gateway, call


def _ollama_race(token: MutableCancellationToken):
    call = ScriptedCall([Invoke(_cancel_then(token, "late answer"))])

    def generate_factory(*_args, **_kwargs):
        call.last_usage = {}
        call.last_response_meta = {}
        return call

    gateway = OllamaGateway(
        target_resolver=lambda *_args, **_kwargs: ModelTarget(
            "local", False, "code", False
        ),
        generate_factory=generate_factory,
    )
    return gateway, call


@pytest.mark.parametrize(
    "gateway_factory",
    [_injected_race, _openai_race, _ollama_race],
    ids=["injected", "openai-compatible", "ollama"],
)
def test_model_response_is_discarded_when_cancellation_wins_return_race(
    gateway_factory, monkeypatch
):
    token = MutableCancellationToken()
    gateway, call = gateway_factory(token)
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.ollama_gateway.ollama_endpoint.normalize",
        lambda *_args: "http://127.0.0.1:11434",
    )

    with pytest.raises(Cancelled, match="during"):
        gateway.generate(ModelRequest(prompt="work", tier="code"), _context(token))

    assert len(call.calls) == 1
    assert call.remaining == 0


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (socket.timeout("injected timeout"), DeadlineExceeded),
        (urllib.error.URLError("injected disconnect"), DependencyUnavailable),
    ],
    ids=["timeout", "disconnect"],
)
def test_network_failures_are_single_attempt_and_typed(failure, expected):
    transport = ScriptedCall([Raise(failure)])
    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig("http://127.0.0.1:8080", model="local"),
        transport=transport,
    )

    with pytest.raises(expected):
        gateway.generate(
            ModelRequest(prompt="work", tier="code"),
            _context(MutableCancellationToken()),
        )

    assert len(transport.calls) == 1
    assert transport.remaining == 0


def test_malformed_model_response_is_not_retried():
    transport = ScriptedCall([Return({"choices": []})])
    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig("http://127.0.0.1:8080", model="local"),
        transport=transport,
    )

    with pytest.raises(DependencyUnavailable, match="no choices"):
        gateway.generate(
            ModelRequest(prompt="work", tier="code"),
            _context(MutableCancellationToken()),
        )

    assert len(transport.calls) == 1
    assert transport.remaining == 0


def test_sqlite_lock_is_retryable_and_does_not_poison_reopened_operation(tmp_path):
    connector = SQLiteFaultConnector()
    registry = SQLiteDurableJobRegistry(
        tmp_path / "jobs.db", connect_factory=connector
    )
    connector.fail_next(
        "execute",
        sqlite3.OperationalError("database is locked"),
        statement_contains="SELECT job_id",
    )

    with pytest.raises(ConcurrencyConflict) as caught:
        registry.get("missing")

    assert caught.value.code == "CONCURRENCY_CONFLICT"
    assert "jobs.db" not in str(caught.value)
    assert registry.get("missing") is None


def test_sqlite_disk_full_aborts_transaction_without_partial_job(tmp_path):
    connector = SQLiteFaultConnector()
    registry = SQLiteDurableJobRegistry(
        tmp_path / "jobs.db", connect_factory=connector
    )
    connector.fail_next(
        "execute",
        sqlite3.OperationalError("database or disk is full"),
        statement_contains="INSERT INTO durable_job",
    )

    with pytest.raises(CapacityExceeded) as caught:
        registry.start(_identity("disk-full"))

    assert caught.value.code == "CAPACITY_EXCEEDED"
    assert registry.get("disk-full") is None


def test_sqlite_corruption_has_non_retryable_integrity_diagnostic(tmp_path):
    connector = SQLiteFaultConnector()
    registry = SQLiteDurableJobRegistry(
        tmp_path / "jobs.db", connect_factory=connector
    )
    connector.fail_next(
        "execute",
        sqlite3.DatabaseError("database disk image is malformed"),
        statement_contains="SELECT job_id",
    )

    with pytest.raises(IntegrityFailure) as caught:
        registry.get("missing")

    assert caught.value.retryable is False
    assert caught.value.code == "INTEGRITY_FAILURE"


class _Cleanup:
    def __init__(self, receipt_factory):
        self._receipt_factory = receipt_factory

    def cleanup(self, request):
        return self._receipt_factory(request)


def test_restart_recovery_rejects_cleanup_receipt_for_another_job(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    registry.start(_identity("recover-me"), process_id=44, process_group_id=44)
    registry.transition("recover-me", JobStatus.RUNNING)
    supervisor = _Cleanup(
        lambda _request: ProcessTreeCleanupReceipt(
            "different-job", True, 1, 1, True
        )
    )

    with pytest.raises(ValueError, match="wrong job"):
        registry.reconcile_with_cleanup(
            supervisor, owner_instance_id="crashed", owner_alive=False
        )

    assert registry.poll("recover-me").status is JobStatus.RUNNING


@pytest.mark.parametrize(
    "receipt",
    [
        lambda: ProcessTreeCleanupReceipt("job", False, 0, 0, True),
        lambda: ProcessTreeCleanupReceipt("job", True, 2, 1, True),
    ],
)
def test_cleanup_receipt_cannot_overstate_completion(receipt):
    with pytest.raises(ValueError, match="complete cleanup"):
        receipt()


class _OutputFailureRegistry(DurableJobRegistry):
    def append_output(self, *args, **kwargs):
        raise CapacityExceeded("injected full store")


def test_output_storage_failure_prevents_false_process_success(tmp_path):
    process = ScriptedProcess(stdout=io.StringIO("important output\n"))
    registry = _OutputFailureRegistry()
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(
            lambda request: ProcessTreeCleanupReceipt(
                request.job_id, True, 0, 0, True
            )
        ),
        launcher=lambda *_args, **_kwargs: process,
        platform_name="posix",
    )
    provider.start(
        ProcessJobRequest(
            _identity("output-full"), ("ignored",), cwd=Path(tmp_path)
        )
    )

    result = provider.wait("output-full", timeout=1)

    assert result.exit_code == 0
    assert result.record.status is JobStatus.FAILED
    assert result.record.error == (
        "process output persistence failed (CapacityExceeded)"
    )


class _CommunicatingProcess(ScriptedProcess):
    def communicate(self, timeout=None):
        self.returncode = 0
        return "important output\n", ""


def test_output_storage_failure_is_captured_on_communicate_fallback(tmp_path):
    process = _CommunicatingProcess()
    registry = _OutputFailureRegistry()
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(
            lambda request: ProcessTreeCleanupReceipt(
                request.job_id, True, 0, 0, True
            )
        ),
        launcher=lambda *_args, **_kwargs: process,
        platform_name="posix",
    )
    provider.start(
        ProcessJobRequest(
            _identity("output-full-fallback"),
            ("ignored",),
            cwd=Path(tmp_path),
        )
    )

    result = provider.wait("output-full-fallback", timeout=1)

    assert result.exit_code == 0
    assert result.record.status is JobStatus.FAILED
    assert "CapacityExceeded" in result.record.error


def test_worker_crash_is_durable_failure_not_success(tmp_path):
    process = ScriptedProcess(waits=[Return(137)])
    registry = DurableJobRegistry()
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(
            lambda request: ProcessTreeCleanupReceipt(
                request.job_id, True, 0, 0, True
            )
        ),
        launcher=lambda *_args, **_kwargs: process,
        platform_name="posix",
    )
    provider.start(
        ProcessJobRequest(
            _identity("crashed-worker"), ("ignored",), cwd=Path(tmp_path)
        )
    )

    result = provider.wait("crashed-worker", timeout=1)

    assert result.exit_code == 137
    assert result.record.status is JobStatus.FAILED
    assert result.record.error == "process exited with a non-zero status"


def test_cancellation_wins_process_completion_race(tmp_path):
    registry = DurableJobRegistry()

    def cancel_then_exit(**_kwargs):
        registry.cancel("cancel-race", reason="operator stop")
        return 0

    process = ScriptedProcess(waits=[Invoke(cancel_then_exit)])
    provider = SubprocessJobProvider(
        registry,
        process_cleanup=_Cleanup(
            lambda request: ProcessTreeCleanupReceipt(
                request.job_id, True, 0, 0, True
            )
        ),
        launcher=lambda *_args, **_kwargs: process,
        platform_name="posix",
    )
    provider.start(
        ProcessJobRequest(
            _identity("cancel-race"), ("ignored",), cwd=Path(tmp_path)
        )
    )

    result = provider.wait("cancel-race", timeout=1)

    assert result.exit_code == 0
    assert result.record.status is JobStatus.CANCELLED
    assert result.record.error == "operator stop"
