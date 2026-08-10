import pytest

import server
from sonder_runtime.adapters.strangler_services import LegacyModelGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import Cancelled, DeadlineExceeded


class _Cancelled:
    cancelled = True

    def wait(self, timeout=None):
        return True


def test_generate_rejects_cancelled_context_before_legacy_call(monkeypatch):
    called = []
    monkeypatch.setattr(server, "sonder", lambda *args, **kwargs: called.append(args) or "x")
    context = local_owner_context(
        correlation_id="legacy-cancelled", cancellation=_Cancelled()
    )

    with pytest.raises(Cancelled):
        LegacyModelGateway().generate(ModelRequest("prompt", "code"), context)
    assert called == []


def test_generate_rejects_expired_context_before_legacy_call(monkeypatch):
    called = []
    monkeypatch.setattr(server, "sonder", lambda *args, **kwargs: called.append(args) or "x")
    context = local_owner_context(
        correlation_id="legacy-expired", timeout_seconds=0.0
    )

    with pytest.raises(DeadlineExceeded):
        LegacyModelGateway().generate(ModelRequest("prompt", "code"), context)
    assert called == []
