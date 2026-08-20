from sonder_runtime.application.training.deployment_rollback import (
    DeploymentRollbackService,
    HealthReport,
    ImmutableArtifact,
    InMemoryDeploymentRepository,
)
from sonder_runtime.domain.common.errors import Conflict, Forbidden


def _service(healthy=True):
    repo = InMemoryDeploymentRepository()
    repo.add_artifact(ImmutableArtifact.create("a1", "model", "r1", b"one"))
    repo.add_artifact(ImmutableArtifact.create("a2", "model", "r2", b"two"))
    return repo, DeploymentRollbackService(repo, lambda _: HealthReport(healthy))


def test_activation_requires_attendance_and_retains_prior_route():
    repo, service = _service()
    try:
        service.activate("a1")
    except Forbidden:
        pass
    else:
        raise AssertionError("activation must be attended")
    first = service.activate("a1", attended=True)
    second = service.activate("a2", attended=True)
    assert second.generation == 2
    assert repo.history()[-1].prior_route_id == first.route_id


def test_unhealthy_candidate_does_not_replace_active_route():
    repo, service = _service()
    first = service.activate("a1", attended=True)
    unhealthy = DeploymentRollbackService(repo, lambda _: HealthReport(False, reason="smoke failed"))
    try:
        unhealthy.activate("a2", attended=True)
    except Conflict:
        pass
    else:
        raise AssertionError("health gate must reject")
    assert repo.active() == first


def test_standalone_rollback_restores_retained_route_and_records_history():
    repo, service = _service()
    first = service.activate("a1", attended=True)
    service.activate("a2", attended=True)
    restored = service.rollback(attended=True, reason="regression")
    assert restored.artifact_id == "a1"
    assert restored.route_id == first.route_id
    assert service.history()[-1].operation == "rollback"
    assert service.history()[-1].reason == "regression"


def test_artifacts_are_content_immutable():
    repo = InMemoryDeploymentRepository()
    artifact = ImmutableArtifact.create("a1", "model", "r1", b"one")
    repo.add_artifact(artifact)
    try:
        repo.add_artifact(ImmutableArtifact.create("a1", "model", "r1", b"changed"))
    except Conflict:
        pass
    else:
        raise AssertionError("artifact replacement must be rejected")
