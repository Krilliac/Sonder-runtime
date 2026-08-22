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
from sonder_runtime.platform.config import SonderConfig

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


def test_build_application_retains_exact_typed_config_and_uses_its_profile():
    config = SonderConfig(profile="server-private")

    application = bootstrap_app.build_application(config=config)

    assert application.config is config
    assert application.profile == "server-private"


def test_default_app_passes_typed_config_to_the_lazy_lifecycle():
    config = SonderConfig(profile="server-private")
    bootstrap_app.reset_for_tests()

    application = bootstrap_app.default_app(config=config)

    assert application.config is config
    assert bootstrap_app.default_app() is application


def test_typed_config_reaches_lazy_runtime_lifecycle():
    from sonder_runtime.adapters.web import lifecycle
    from sonder_runtime.platform.config import (
        CapacityConfig,
        ObservabilityConfig,
    )

    config = SonderConfig(
        capacity=CapacityConfig(http_requests=7, queue_depth=9),
        observability=ObservabilityConfig(metrics_enabled=False),
    )
    lifecycle.reset_for_tests()
    bootstrap_app.build_application(config=config)

    assert lifecycle._instance is None
    instance = lifecycle.get()
    assert instance._max_concurrent == 7
    assert instance._queue_depth == 9
    assert instance.metrics.enabled is False
    lifecycle.reset_for_tests()


def test_profile_only_application_keeps_compatibility_shape():
    application = bootstrap_app.build_application("workstation-local")

    assert application.profile == "workstation-local"
    assert application.config is None


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


def test_composition_root_exposes_typed_web_provider():
    from sonder_runtime.adapters.web_provider import LegacyWebProvider
    from sonder_runtime.application.ports.web import WebProvider

    application = bootstrap_app.build_application()

    assert isinstance(application.web_provider, LegacyWebProvider)
    assert callable(WebProvider.request)
    assert callable(WebProvider.health)
    assert callable(application.web_provider.request)
    assert callable(application.web_provider.health)


def test_composition_root_exposes_redacted_provider_health_projection():
    application = bootstrap_app.build_application()

    rows = application.provider_health_data()

    assert rows
    assert {row["provider_id"] for row in rows} >= {"embedding"}
    assert all(set(row) == {"provider_id", "status", "detail", "checked_at"} for row in rows)
    assert all(row["status"] in {"healthy", "degraded", "unhealthy", "unknown"} for row in rows)


def test_composition_root_exposes_cooperative_provider_cancellation():
    application = bootstrap_app.build_application()

    assert application.cancel_provider("embedding", reason="test stop") is False


def test_composition_root_publishes_attended_training_and_update_lanes():
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.specialized_lifecycle import (
        ActivationRequest, ActivationResult, DeploymentResult, TrainingRequest,
    )

    class Training:
        def train(self, request, context):
            assert request.run_id == "run-1"
            assert not context.cancellation.cancelled
            return DeploymentResult(
                "training", request.run_id, "deployment-1", "model-1", "a" * 64,
            )

    class Update:
        def activate(self, request, context):
            assert request.activation_id == "activation-1"
            assert not context.cancellation.cancelled
            return ActivationResult(
                "update", request.activation_id, request.release_id,
                request.version, request.artifact_digest,
            )

    application = bootstrap_app.build_application(
        training_backend=Training(), update_activator=Update(),
    )
    try:
        assert {row.provider_id for row in application.provider_health()} == {
            "embedding", "training", "update",
        }
        context = local_owner_context(correlation_id="composition-specialized")
        deployment = application.train_provider(
            TrainingRequest("run-1", "base-model", "rev-1", "d" * 64), context,
        )
        assert deployment.provider_id == "training"
        activation = application.activate_provider(
            ActivationRequest("activation-1", "release-1", "1.0.0", "a" * 64), context,
        )
        assert activation.provider_id == "update"
        assert application.cancel_provider("training", reason="operator stop") is False
    finally:
        application.close_providers(timeout=1)


def test_composition_root_specialized_operations_fail_closed_when_lane_is_absent():
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.specialized_lifecycle import TrainingRequest
    from sonder_runtime.application.providers import ProviderLifecycleError

    application = bootstrap_app.build_application()
    with pytest.raises(ProviderLifecycleError, match="unknown provider"):
        application.train_provider(
            TrainingRequest("run-1", "base-model", "rev-1", "d" * 64),
            local_owner_context(correlation_id="composition-absent"),
        )


def test_composition_root_exposes_lazy_cached_durable_session_repository(
    tmp_path, monkeypatch
):
    database = tmp_path / "sessions.db"
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(database))
    application = bootstrap_app.build_application()

    assert not database.exists()
    first = application.session_repository()
    second = application.session_repository()

    from sonder_runtime.adapters.persistence.session_repository import (
        SQLiteSessionRepository,
    )

    assert isinstance(first, SQLiteSessionRepository)
    assert first is second
    assert database.exists()


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
