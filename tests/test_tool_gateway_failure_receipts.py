"""A call that ends in an exception leaves one honest, redacted receipt.

The sink and the audit used here store exactly what the gateway hands them,
so nothing downstream can hide an unredacted error or a false flag.
"""
from __future__ import annotations

import logging
import time

import pytest

from sonder_runtime.adapters.persistence.tool_audit import DurableToolAuditRepository
from sonder_runtime.application.tools.facade import (
    IdentityRedactor,
    PatternOutputRedactor,
)
from sonder_runtime.application.tools.gateway_contract import (
    MAX_EXIT_TEXT_REDACTION_CHARS,
    RedactedOutput,
    ToolGateway,
    ToolGatewayRequest,
    ToolInvocationOutput,
    ToolPermission,
    ToolReceipt,
    ToolScope,
)
from sonder_runtime.domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    Forbidden,
    InvalidInput,
)
from sonder_runtime.domain.security.redaction import REDACTION_FAILED
from sonder_runtime.platform.logging import Redactor

CANARY = "sk-CANARY123"
RAW = "refused before execution: /fake/home/project/%s.env" % CANARY


class Schema:
    def __init__(self, error=None):
        self.error = error

    def validate(self, *_args):
        if self.error is not None:
            raise self.error


class Permissions:
    def __init__(self, error=None):
        self.error = error

    def authorize(self, _name, _scope, _permission):
        if self.error is not None:
            raise self.error
        return "host:test"


class Approvals:
    def approve(self, _request):
        return True


class Invoker:
    def __init__(self, error=None):
        self.error, self.calls = error, 0

    def invoke(self, _request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ToolInvocationOutput(True, "ok")


class CanaryRedactor:
    """Replaces the canary and reports honestly whether it changed anything."""

    def __init__(self):
        self.seen = []

    def redact(self, _tool, value):
        self.seen.append(value)
        if not isinstance(value, str):
            return RedactedOutput(value, applied=False)
        safe = value.replace(CANARY, "[redacted]")
        return RedactedOutput(safe, applied=safe != value)


class Receipts:
    def __init__(self):
        self.items = []

    def record(self, receipt):
        self.items.append(receipt)


class Audit:
    def __init__(self, error=None):
        self.error, self.items = error, []

    def append(self, _request, receipt):
        if self.error is not None:
            raise self.error
        self.items.append(receipt)


class Cancel:
    cancelled = True


def build(*, schema=None, permissions=None, invoker=None, redactor=None, audit=None):
    receipts = Receipts()
    audit = Audit() if audit is None else audit
    invoker = invoker or Invoker()
    gateway = ToolGateway(
        schema or Schema(), permissions or Permissions(), Approvals(), invoker,
        redactor or CanaryRedactor(), receipts, audit=audit,
    )
    return gateway, receipts, audit, invoker


def request(**kwargs):
    return ToolGatewayRequest(
        request_id="r1", tool_name="read", arguments={"path": "x"},
        scope=ToolScope("owner", ("project",), frozenset({"read"})),
        permission=ToolPermission(frozenset({"read"})),
        **kwargs,
    )


def published(receipts, audit):
    """The single receipt both sinks hold."""
    assert len(receipts.items) == 1 and audit.items == receipts.items
    return receipts.items[0]


def test_permission_denial_text_is_redacted_before_publication():
    redactor = CanaryRedactor()
    gateway, receipts, audit, invoker = build(
        permissions=Permissions(Forbidden(RAW)), redactor=redactor)
    with pytest.raises(Forbidden):
        gateway.execute(request())
    receipt = published(receipts, audit)
    assert redactor.seen == [RAW]
    assert CANARY not in receipt.error and "[redacted]" in receipt.error
    assert receipt.redaction_applied is True
    assert (receipt.terminal, receipt.error_code) == ("policy_denied", "Forbidden")
    assert receipt.effect_certainty == "not_started" and invoker.calls == 0


def test_redaction_flag_is_false_when_the_redactor_changed_nothing():
    gateway, receipts, audit, _ = build(
        permissions=Permissions(Forbidden("tool approval is required")),
        redactor=IdentityRedactor())
    with pytest.raises(Forbidden):
        gateway.execute(request())
    receipt = published(receipts, audit)
    assert receipt.error == "tool approval is required"
    assert receipt.redaction_applied is False
    assert receipt.evidence == {}


def test_plain_redactor_return_counts_as_redacted():
    class Plain:
        def redact(self, _tool, value):
            return value.replace(CANARY, "[redacted]")

    gateway, receipts, audit, _ = build(
        permissions=Permissions(Forbidden(RAW)), redactor=Plain())
    with pytest.raises(Forbidden):
        gateway.execute(request())
    receipt = published(receipts, audit)
    assert CANARY not in receipt.error and receipt.redaction_applied is True


class RaisingRedactor:
    def redact(self, _tool, value):
        raise RuntimeError("cannot redact %s" % value)


class FailedRedactor:
    def redact(self, _tool, _value):
        return RedactedOutput(REDACTION_FAILED, applied=True)


class NonTextRedactor:
    def redact(self, _tool, value):
        return RedactedOutput({"raw": value}, applied=True)


@pytest.mark.parametrize("redactor", [RaisingRedactor, FailedRedactor, NonTextRedactor])
def test_failed_redaction_publishes_only_a_constant_summary(redactor, caplog):
    gateway, receipts, audit, _ = build(
        permissions=Permissions(Forbidden(RAW)), redactor=redactor())
    with caplog.at_level(logging.DEBUG), pytest.raises(Forbidden):
        gateway.execute(request())
    receipt = published(receipts, audit)
    assert receipt.error == "tool call ended as policy_denied (Forbidden); details withheld"
    assert receipt.redaction_applied is True
    assert receipt.evidence["error_redaction"] == "fallback"
    diagnostic_id = receipt.evidence["diagnostic_id"]
    assert diagnostic_id and diagnostic_id in caplog.text
    assert CANARY not in caplog.text and CANARY not in repr(receipt)


@pytest.mark.parametrize("error, terminal", [
    (Cancelled(RAW), "cancelled"),
    (DeadlineExceeded(RAW), "deadline_exceeded"),
    (Forbidden(RAW), "policy_denied"),
])
def test_caller_facing_exit_raised_inside_the_invoker_is_redacted_and_uncertain(error, terminal):
    redactor = CanaryRedactor()
    gateway, receipts, audit, invoker = build(invoker=Invoker(error), redactor=redactor)
    with pytest.raises(type(error)):
        gateway.execute(request())
    receipt = published(receipts, audit)
    assert redactor.seen == [RAW] and invoker.calls == 1
    assert CANARY not in receipt.error and receipt.redaction_applied is True
    assert receipt.terminal == terminal
    # The tool was entered: nothing here can say its effect did not happen.
    assert receipt.effect_certainty == "uncertain"


@pytest.mark.parametrize("kwargs, error, terminal", [
    ({"cancellation": Cancel()}, Cancelled, "cancelled"),
    ({"deadline_monotonic": time.monotonic() - 1}, DeadlineExceeded, "deadline_exceeded"),
])
def test_control_exit_passes_through_the_redactor(kwargs, error, terminal):
    redactor = CanaryRedactor()
    gateway, receipts, audit, invoker = build(redactor=redactor)
    with pytest.raises(error):
        gateway.execute(request(**kwargs))
    receipt = published(receipts, audit)
    assert redactor.seen == [receipt.error] and receipt.redaction_applied is False
    assert receipt.terminal == terminal and receipt.effect_certainty == "not_started"
    assert invoker.calls == 0


def test_long_denial_text_is_bounded_after_redaction():
    gateway, receipts, audit, _ = build(
        permissions=Permissions(Forbidden("x" * 5000 + CANARY)))
    with pytest.raises(Forbidden):
        gateway.execute(request())
    receipt = published(receipts, audit)
    assert len(receipt.error) == 2000 and CANARY not in receipt.error


def test_audit_failure_on_a_denial_does_not_replace_the_denial(caplog):
    denial = Forbidden(RAW)
    audit_error = OSError("audit unavailable")
    gateway, receipts, _, _ = build(
        permissions=Permissions(denial), audit=Audit(audit_error))
    with caplog.at_level(logging.ERROR), pytest.raises(Forbidden) as caught:
        gateway.execute(request())
    assert caught.value is denial and caught.value.__cause__ is audit_error
    # Audit first: what could not be made durable is not published anywhere.
    assert receipts.items == []
    assert "OSError" in caplog.text and CANARY not in caplog.text


@pytest.mark.parametrize("error", [
    RuntimeError("canary-SECRET-123"), KeyError("canary-SECRET-123"),
    InvalidInput("canary-SECRET-123"), TypeError("canary-SECRET-123"),
])
def test_ordinary_invoker_exception_publishes_a_failed_receipt(error):
    redactor = CanaryRedactor()
    gateway, receipts, audit, invoker = build(invoker=Invoker(error), redactor=redactor)
    with pytest.raises(type(error)) as caught:
        gateway.execute(request())
    assert caught.value is error and invoker.calls == 1
    receipt = published(receipts, audit)
    assert receipt.success is False and receipt.terminal == "failed"
    assert receipt.error_code == type(error).__name__
    # Unvetted exception text is never published, redactor or not.
    assert redactor.seen == [] and "canary-SECRET-123" not in repr(receipt)
    assert receipt.error == (
        "tool call ended as failed (%s); details withheld" % type(error).__name__)
    assert receipt.redaction_applied is True
    assert receipt.effect_certainty == "uncertain"


def test_schema_refusal_publishes_a_failed_receipt_for_a_call_that_never_started():
    gateway, receipts, audit, invoker = build(
        schema=Schema(InvalidInput("unknown argument canary-SECRET-123")))
    with pytest.raises(InvalidInput):
        gateway.execute(request())
    receipt = published(receipts, audit)
    assert (receipt.terminal, receipt.error_code) == ("failed", "InvalidInput")
    assert receipt.effect_certainty == "not_started" and invoker.calls == 0
    assert "canary-SECRET-123" not in repr(receipt)


def test_permission_evaluator_crash_publishes_a_failed_receipt():
    gateway, receipts, audit, invoker = build(
        permissions=Permissions(RuntimeError("canary-SECRET-123")))
    with pytest.raises(RuntimeError):
        gateway.execute(request())
    receipt = published(receipts, audit)
    assert (receipt.terminal, receipt.effect_certainty) == ("failed", "not_started")
    assert invoker.calls == 0 and "canary-SECRET-123" not in repr(receipt)


def test_audit_failure_on_an_invoker_exception_does_not_replace_it():
    error = RuntimeError("tool crashed")
    audit_error = OSError("audit unavailable")
    gateway, receipts, _, _ = build(invoker=Invoker(error), audit=Audit(audit_error))
    with pytest.raises(RuntimeError) as caught:
        gateway.execute(request())
    assert caught.value is error and caught.value.__cause__ is audit_error
    assert receipts.items == []


def test_publication_failure_keeps_an_existing_cause():
    root = ValueError("root cause")
    error = RuntimeError("tool crashed")
    error.__cause__ = root
    gateway, _, _, _ = build(invoker=Invoker(error), audit=Audit(OSError("audit unavailable")))
    with pytest.raises(RuntimeError) as caught:
        gateway.execute(request())
    assert caught.value is error and caught.value.__cause__ is root


def test_interrupt_that_is_not_an_exception_publishes_nothing():
    class Halt(BaseException):
        pass

    gateway, receipts, audit, _ = build(invoker=Invoker(Halt()))
    with pytest.raises(Halt):
        gateway.execute(request())
    assert receipts.items == [] and audit.items == []


class OutputRaisingRedactor:
    """Fails only on the invoker's output, after the effect has happened."""

    def __init__(self, error):
        self.error, self.seen = error, []

    def redact(self, _tool, value):
        self.seen.append(value)
        raise self.error


@pytest.mark.parametrize("error", [
    RuntimeError("cannot redact canary-SECRET-123"),
    # Even a caller-facing type raised here is a gateway fault: the invoker
    # already returned, so the call did not end as cancelled.
    Cancelled("cannot redact canary-SECRET-123"),
])
def test_output_redaction_failure_after_invoke_publishes_an_uncertain_failure(error):
    redactor = OutputRaisingRedactor(error)
    gateway, receipts, audit, invoker = build(redactor=redactor)
    with pytest.raises(type(error)) as caught:
        gateway.execute(request())
    assert caught.value is error and invoker.calls == 1
    receipt = published(receipts, audit)
    assert (receipt.success, receipt.terminal, receipt.effect_certainty) == (
        False, "failed", "uncertain")
    assert receipt.error_code == type(error).__name__
    assert receipt.error == (
        "tool call ended as failed (%s); details withheld" % type(error).__name__)
    assert receipt.output == "" and receipt.policy_match == "host:test"
    # The fault text is never redacted into the receipt, only withheld.
    assert redactor.seen == ["ok"] and "canary-SECRET-123" not in repr(receipt)


def test_receipt_construction_failure_after_invoke_publishes_an_uncertain_failure():
    class Unhashable(Invoker):
        def invoke(self, _request):
            self.calls += 1
            # Mixed key types cannot be canonically digested.
            return ToolInvocationOutput(True, {1: "a", "b": 2})

    gateway, receipts, audit, invoker = build(
        invoker=Unhashable(), redactor=IdentityRedactor())
    with pytest.raises(TypeError):
        gateway.execute(request())
    receipt = published(receipts, audit)
    assert invoker.calls == 1
    assert (receipt.terminal, receipt.effect_certainty, receipt.error_code) == (
        "failed", "uncertain", "TypeError")


def test_post_invoke_failure_receipt_publication_error_keeps_the_original():
    error = RuntimeError("redaction unavailable")
    audit_error = OSError("audit unavailable")
    gateway, receipts, _, invoker = build(
        redactor=OutputRaisingRedactor(error), audit=Audit(audit_error))
    with pytest.raises(RuntimeError) as caught:
        gateway.execute(request())
    assert caught.value is error and caught.value.__cause__ is audit_error
    assert invoker.calls == 1 and receipts.items == []


def test_outcome_publication_failure_adds_no_second_receipt():
    class SinkFails(Receipts):
        def record(self, receipt):
            super().record(receipt)
            raise OSError("sink unavailable")

    receipts, audit = SinkFails(), Audit()
    gateway = ToolGateway(Schema(), Permissions(), Approvals(), Invoker(),
                          CanaryRedactor(), receipts, audit=audit)
    with pytest.raises(OSError):
        gateway.execute(request())
    # The completed receipt is already durable; a failure receipt under the
    # same request id would make the audit ambiguous.
    assert [item.terminal for item in audit.items] == ["completed"]
    assert receipts.items == audit.items


def test_completed_receipt_reports_a_settled_effect():
    gateway, receipts, audit, _ = build()
    receipt = gateway.execute(request())
    assert published(receipts, audit) is receipt
    assert receipt.success and receipt.effect_certainty == "settled"


def test_receipt_rejects_an_unknown_effect_certainty():
    with pytest.raises(InvalidInput, match="effect_certainty"):
        ToolReceipt("r1", "read", False, effect_certainty="probably")


def test_durable_audit_records_effect_certainty(tmp_path):
    durable = DurableToolAuditRepository(tmp_path / "tool-audit.jsonl")
    gateway, _, _, _ = build(invoker=Invoker(RuntimeError("tool crashed")), audit=durable)
    with pytest.raises(RuntimeError):
        gateway.execute(request())
    durable.verify()
    (entry,) = durable.read()
    assert (entry["terminal"], entry["effect_certainty"]) == ("failed", "uncertain")
    assert entry["error"] == "tool call ended as failed (RuntimeError); details withheld"


def test_secret_straddling_the_error_bound_is_redacted_in_receipt_and_durable_audit(tmp_path):
    # Production wiring: the platform value redactor behind the gateway's
    # pattern redactor, and the same value redactor in the durable audit.
    secret = "LiveSecretValue12345"
    value_redactor = Redactor(secret_values=[secret], env={})
    durable = DurableToolAuditRepository(tmp_path / "tool-audit.jsonl", redactor=value_redactor)
    message = "x" * 1985 + secret + " trailing"
    gateway, receipts, _, _ = build(
        permissions=Permissions(Forbidden(message)),
        redactor=PatternOutputRedactor(value_redactor.redact), audit=durable)
    with pytest.raises(Forbidden):
        gateway.execute(request())
    (receipt,) = receipts.items
    (entry,) = durable.read()
    for error in (receipt.error, entry["error"]):
        assert len(error) <= 2000
        # No fragment of the secret survives the cut, not even its prefix.
        assert secret[:8] not in error
    assert receipt.redaction_applied is True


def test_exit_text_beyond_the_redaction_bound_publishes_only_the_summary():
    redactor = CanaryRedactor()
    gateway, receipts, audit, _ = build(
        permissions=Permissions(Forbidden("x" * MAX_EXIT_TEXT_REDACTION_CHARS + CANARY)),
        redactor=redactor)
    with pytest.raises(Forbidden):
        gateway.execute(request())
    receipt = published(receipts, audit)
    assert redactor.seen == []
    assert receipt.error == "tool call ended as policy_denied (Forbidden); details withheld"
    assert receipt.evidence["error_redaction"] == "fallback"
