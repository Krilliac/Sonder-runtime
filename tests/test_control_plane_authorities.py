from sonder_runtime.application.extensions.experiments import EphemeralExperimentManager
from sonder_runtime.application.selfmod.selfmod_service import GuardedLegacySelfmodService


def test_experiment_snapshot_is_stable_and_bounded(tmp_path):
    manager = EphemeralExperimentManager(
        lambda _definition: False,
        host_factory=lambda *_args: None,
        temp_root=tmp_path,
    )
    try:
        manager.define("b", ["python", "-V"])
        manager.define("a", ["python", "-V"])
        assert [item.experiment_id for item in manager.snapshot()] == ["a", "b"]
    finally:
        manager.close()


def test_selfmod_run_projection_delegates_bounded_summaries():
    class Legacy:
        def list_runs(self, limit=20):
            assert limit == 64
            return [{"id": "run-1", "phase": "proposed", "objective": "safe"}]

    service = GuardedLegacySelfmodService(Legacy())
    assert service.list_runs() == ({"id": "run-1", "phase": "proposed", "objective": "safe"},)
