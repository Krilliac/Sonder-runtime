"""SPEC-3 M1/M2: composition root, operation context, policy use cases."""
from __future__ import annotations

import threading

import pytest

from sonder_runtime.application.context import (
    LOCAL_OWNER,
    local_owner_context,
)
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.common.errors import (
    ConcurrencyConflict,
    InvalidInput,
)
from sonder_runtime.domain.runtime_policy import rules

pytestmark = pytest.mark.integration


@pytest.fixture()
def application(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json")
    )
    bootstrap_app.reset_for_tests()
    yield bootstrap_app.build_application()
    bootstrap_app.reset_for_tests()


def _context(**kwargs):
    return local_owner_context(correlation_id="req_test", **kwargs)


def test_build_application_requires_known_profile():
    with pytest.raises(ValueError, match="unknown profile"):
        bootstrap_app.build_application("public-saas")


def test_composition_root_uses_canonical_system_clock_adapter():
    from sonder_runtime.adapters.system_clock import SystemClock
    application = bootstrap_app.build_application()

    assert type(application.clock) is SystemClock


def test_composition_root_uses_canonical_evaluation_history_adapter():
    from sonder_runtime.adapters.evaluation_history_reader import (
        EvaluationHistoryReaderAdapter,
    )

    application = bootstrap_app.build_application()

    assert type(application.evaluation_history._reader) is EvaluationHistoryReaderAdapter


def test_importing_bootstrap_has_no_side_effects(tmp_path, monkeypatch):
    # The composition root builds lazily: constructing the graph must not
    # create the policy file; only first use does.
    policy_file = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_file))
    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application()
    assert not policy_file.exists()
    application.runtime_policy.get(_context())
    assert policy_file.exists()


def test_policy_roundtrip_through_use_case(application):
    context = _context()
    view = application.runtime_policy.get(context)
    assert view.local_models["fast"]
    updated = application.runtime_policy.update(
        context,
        routing={"router": "code"},
        expected_revision=view.revision,
    )
    assert updated.routing["router"] == "code"
    assert updated.revision == view.revision + 1


def test_policy_update_conflicts_map_to_domain_errors(application):
    context = _context()
    view = application.runtime_policy.get(context)
    with pytest.raises(ConcurrencyConflict):
        application.runtime_policy.update(
            context,
            routing={"router": "code"},
            expected_revision=view.revision + 41,
        )
    with pytest.raises(InvalidInput):
        application.runtime_policy.update(
            context, local_models={"fast": "sonder-cloud:latest"}
        )


def test_policy_cannot_widen_permissions(application):
    # The use case only accepts models/routing/npu; there is no channel
    # for roots, credentials, or cloud consent (R: runtime policy cannot
    # broaden permissions).
    import inspect

    signature = inspect.signature(application.runtime_policy.update)
    assert set(signature.parameters) == {
        "context", "local_models", "routing", "npu", "expected_revision"
    }


def test_domain_rules_reject_cloud_and_personal_aliases():
    with pytest.raises(ValueError, match="cloud"):
        rules.validate_model("qwen-cloud:latest", "fallback")
    assert (
        rules.validate_model("sonder-personal", "x")
        == rules.RESERVED_PERSONAL_MODEL
    )
    assert rules.npu_mode("routing", {"npu": {"mode": "prefer"}}) == "prefer"
    assert rules.npu_mode("unknown-capability", {"npu": {"mode": "prefer"}}) == "off"


def test_operation_context_deadline_and_identity():
    context = local_owner_context(
        correlation_id="req_x", timeout_seconds=30.0
    )
    assert context.principal_id == LOCAL_OWNER
    assert 0 < context.remaining_seconds <= 30.0
    assert not context.expired
    assert not context.cancellation.cancelled
    expired = local_owner_context(
        correlation_id="req_y", timeout_seconds=0.0
    )
    assert expired.expired


def test_default_cancellation_wait_honours_positive_timeout(monkeypatch):
    sleeps = []
    context = _context()
    monkeypatch.setattr(
        "sonder_runtime.application.context.time.sleep", sleeps.append
    )

    assert context.cancellation.wait(0.5) is False

    assert sleeps == [0.5]


def test_default_app_build_is_atomic(monkeypatch):
    bootstrap_app.reset_for_tests()
    first_started = threading.Event()
    release_first = threading.Event()
    second_built = threading.Event()
    calls = []

    def build():
        instance = object()
        calls.append(instance)
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(2)
        else:
            second_built.set()
        return instance

    monkeypatch.setattr(bootstrap_app, "build_application", build)
    results = []
    first = threading.Thread(target=lambda: results.append(bootstrap_app.default_app()))
    second = threading.Thread(target=lambda: results.append(bootstrap_app.default_app()))
    first.start()
    assert first_started.wait(2)
    second.start()
    second_built.wait(0.2)
    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert len(calls) == 1
    assert results[0] is results[1]
    bootstrap_app.reset_for_tests()
