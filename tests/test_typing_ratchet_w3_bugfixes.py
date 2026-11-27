"""Targeted regression tests for bugs uncovered during Typing Ratchet Wave 3."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.compute_fabric.http_client import PinnedHttpsClient
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.adapters.process_liveness import _parse_pid
from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway
from sonder_runtime.application.execution.effect_journal import EffectJournalError
from sonder_runtime.application.execution.worker_bindings import _json_safe
from sonder_runtime.application.session.query_export import (
    QueryExportError,
    SessionEventRecord,
)
from sonder_runtime.application.tools.gateway_contract import (
    ToolGateway,
    ToolGatewayRequest,
    ToolInvocationOutput,
    ToolPermission,
    ToolScope,
)
from sonder_runtime.application.tools.typed_gateway import PortInvocationOutput
from sonder_runtime.domain.common.errors import Forbidden


@dataclass(frozen=True)
class _SampleState:
    name: str
    count: int


class _CustomIndex:
    def __init__(self, value: int) -> None:
        self._value = value

    def __index__(self) -> int:
        return self._value


def test_worker_bindings_json_safe_rejects_dataclass_type_with_journal_error() -> None:
    """Dataclass classes must raise EffectJournalError, not raw asdict TypeError."""
    assert _json_safe(_SampleState("ok", 3)) == {"name": "ok", "count": 3}
    with pytest.raises(EffectJournalError, match="not serializable"):
        _json_safe(_SampleState)


def test_port_invocation_output_default_metadata_is_isolated_mapping() -> None:
    """Default metadata must be a Mapping (not an empty tuple) and not shared."""
    first = PortInvocationOutput(success=True)
    second = PortInvocationOutput(success=True)
    assert isinstance(first.metadata, Mapping)
    assert first.metadata.get("missing") is None
    assert dict(first.metadata) == {}
    assert first.metadata is not second.metadata


def test_gateway_execute_admitted_fails_closed_when_traits_unset() -> None:
    """Direct _execute_admitted with traits=None must fail closed instead of AttributeError."""

    class _AllowSchema:
        def validate(self, tool_name: str, arguments: Mapping[str, object], selection: object = None) -> None:
            return None

    class _AllowPermissions:
        def authorize(self, tool_name: str, scope: ToolScope, permission: ToolPermission) -> str:
            return "allow"

    class _DenyApprovals:
        def approve(self, request: ToolGatewayRequest) -> bool:
            return False

    class _Invoker:
        def invoke(self, request: ToolGatewayRequest) -> ToolInvocationOutput:
            return ToolInvocationOutput(success=True, output="ok")

    class _Redactor:
        def redact(self, tool_name: str, output: object) -> object:
            return output

    receipts: list[object] = []

    class _Sink:
        def record(self, receipt: object) -> None:
            receipts.append(receipt)

    gateway = ToolGateway(
        _AllowSchema(),
        _AllowPermissions(),
        _DenyApprovals(),
        _Invoker(),
        _Redactor(),
        _Sink(),  # type: ignore[arg-type]
    )
    request = ToolGatewayRequest(
        request_id="req-1",
        tool_name="demo_tool",
        arguments={},
        scope=ToolScope(principal_id="user-1"),
        permission=ToolPermission(reconciliation="idempotent", traits=None),
    )
    with pytest.raises(Forbidden, match="idempotent=TRUE"):
        gateway._execute_admitted(request)
    assert len(receipts) == 1


@pytest.mark.parametrize("bad_sequence", [True, False, None, "not-an-int", [1], {"a": 1}])
def test_session_event_record_from_dict_rejects_invalid_sequence(bad_sequence: object) -> None:
    raw: dict[str, object] = {
        "session_id": "s-1",
        "sequence": bad_sequence,
        "event_id": "e-1",
        "event_type": "user.message",
        "occurred_at_utc": "2026-11-25T00:00:00Z",
        "payload": {"content": "hi"},
        "event_hash": "a" * 64,
    }
    with pytest.raises(QueryExportError, match="sequence"):
        SessionEventRecord.from_dict(raw)


@pytest.mark.parametrize("bad_prev", [123, ["abc"], {"h": "1"}])
def test_session_event_record_from_dict_rejects_non_string_previous_hash(bad_prev: object) -> None:
    raw: dict[str, object] = {
        "session_id": "s-1",
        "sequence": 1,
        "event_id": "e-1",
        "event_type": "user.message",
        "occurred_at_utc": "2026-11-25T00:00:00Z",
        "payload": {"content": "hi"},
        "previous_hash": bad_prev,
        "event_hash": "a" * 64,
    }
    with pytest.raises(QueryExportError, match="previous_hash"):
        SessionEventRecord.from_dict(raw)


def test_session_event_record_from_dict_accepts_valid_inputs() -> None:
    raw: dict[str, object] = {
        "session_id": "s-1",
        "sequence": "7",
        "event_id": "e-1",
        "event_type": "user.message",
        "occurred_at_utc": "2026-11-25T00:00:00Z",
        "payload": {"content": "hi"},
        "previous_hash": None,
        "event_hash": "a" * 64,
    }
    record = SessionEventRecord.from_dict(raw)
    assert record.sequence == 7
    assert record.previous_hash is None


def test_pinned_https_client_response_length_rejects_bare_string_from_get_all() -> None:
    """get_all('Content-Length') returning a bare string '5' must not match a 5-byte body."""
    bare_string_headers = SimpleNamespace(get_all=lambda name: "5")
    assert PinnedHttpsClient._response_length(bare_string_headers, b"hello") is False

    valid_list_headers = SimpleNamespace(get_all=lambda name: ["5"])
    assert PinnedHttpsClient._response_length(valid_list_headers, b"hello") is True

    duplicate_headers = SimpleNamespace(get_all=lambda name: ["5", "5"])
    assert PinnedHttpsClient._response_length(duplicate_headers, b"hello") is False


def test_model_request_admission_claim_store_creation_rejects_unset_path() -> None:
    admission = HostModelRequestAdmission(config=None)
    with pytest.raises(ValueError, match="not initialized"):
        admission._claim_store_creation()


def test_model_call_error_retry_after_handles_none_and_invalid_values() -> None:
    err_none = ModelCallError("timeout", "timed out", retry_after_seconds=None)
    assert err_none.retry_after_seconds is None

    err_valid = ModelCallError("admission", "busy", retry_after_seconds=2.5)
    assert err_valid.retry_after_seconds == 2.5

    err_nan = ModelCallError("admission", "busy", retry_after_seconds=float("nan"))
    assert err_nan.retry_after_seconds is None


def test_process_liveness_parse_pid_supports_index_and_rejects_invalid() -> None:
    assert _parse_pid(42) == 42
    assert _parse_pid("42") == 42
    assert _parse_pid(_CustomIndex(99)) == 99
    assert _parse_pid(True) is None
    assert _parse_pid(0) is None
    assert _parse_pid(-1) is None
    assert _parse_pid(3.14) is None
    assert _parse_pid(None) is None
    assert _parse_pid(0x1_0000_0000) is None


def test_fallback_gateway_capabilities_normalizes_iterable_and_missing() -> None:
    primary_with_set = SimpleNamespace(capabilities={"chat", "embeddings"})
    fallback = SimpleNamespace()
    gw = PreSendFallbackGateway(primary_with_set, fallback=fallback)  # type: ignore[arg-type]
    assert gw.capabilities == frozenset({"chat", "embeddings"})

    primary_without = SimpleNamespace()
    gw_empty = PreSendFallbackGateway(primary_without, fallback=fallback)  # type: ignore[arg-type]
    assert gw_empty.capabilities == frozenset()
