from sonder_runtime.application.evaluation.trajectory_replay import (
    TrajectoryRecord,
    TrajectoryStep,
    compare_trajectories,
    replay_trajectory,
)


def _record():
    return TrajectoryRecord.from_steps(
        "run-1",
        [
            TrajectoryStep(0, {"prompt": "a"}, {"answer": 1}, {"counter": 1}),
            TrajectoryStep(1, {"prompt": "b"}, {"answer": 2}, {"counter": 2}),
        ],
        metadata={"suite": "unit", "version": 1},
    )


def test_trajectory_digest_and_serialization_are_deterministic():
    first = _record()
    second = TrajectoryRecord.from_steps(
        "run-1", tuple(reversed(tuple(reversed(first.steps)))),
        metadata={"version": 1, "suite": "unit"},
    )
    assert first.digest == second.digest
    assert first.as_dict() == second.as_dict()


def test_replay_reports_equivalence_for_same_outputs():
    report = replay_trajectory(_record(), lambda value: {"answer": 1 if value["prompt"] == "a" else 2})
    assert report.equivalent
    assert report.expected_digest == report.actual_digest


def test_replay_reports_step_and_field_divergence():
    report = replay_trajectory(_record(), lambda value: {"answer": 99} if value["prompt"] == "b" else {"answer": 1})
    assert not report.equivalent
    assert [(item.index, item.field, item.expected, item.actual) for item in report.divergences] == [
        (1, "output", {"answer": 2}, {"answer": 99}),
    ]


def test_compare_detects_metadata_and_length_differences():
    expected = _record()
    actual = TrajectoryRecord.from_steps("run-1", [expected.steps[0]], metadata={"suite": "other"})
    report = compare_trajectories(expected, actual)
    assert not report.equivalent
    assert {(item.index, item.field) for item in report.divergences} == {(-1, "metadata"), (-1, "step_count")}
