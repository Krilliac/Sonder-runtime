"""Offline coverage for the reproducible scenario/provider evaluation stack."""
from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from sonder_runtime.adapters.reproducible_evaluation import (
    DeterministicLocalProvider,
    JsonEvaluationMatrixRepository,
    JsonEvaluationRunRepository,
    ScriptedProviderResult,
    load_provider_fixture,
    load_scenario_fixture,
)
from sonder_runtime.application.evaluation.proposal_lifecycle import EvaluationMode
from sonder_runtime.application.evaluation.reproducible import (
    ErrorCategory,
    EvaluationScenario,
    EvaluationScenarioRegistry,
    OutcomeStatus,
    ProviderIdentity,
    RegressionThresholds,
    ReproducibleEvaluationError,
    ReproducibleEvaluationRunner,
    ScenarioCase,
    evaluation_diagnostics,
)
from sonder_runtime.application.evaluation.trajectory_replay import TrajectoryRecord


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "evaluation"
SCENARIO_PATH = FIXTURES / "scenario.local-tools.v1.json"
PROVIDER_PATH = FIXTURES / "provider.local-reference.v1.json"


def _identity(name: str = "candidate") -> ProviderIdentity:
    return ProviderIdentity("test-local", name, "1", (name[0] * 64) if name[0] in "abcdef" else "a" * 64)


def _provider(scenario: EvaluationScenario, outputs, *, name: str = "candidate") -> DeterministicLocalProvider:
    responses = {
        json.dumps(case.as_dict()["input"], sort_keys=True, separators=(",", ":"), ensure_ascii=False): ScriptedProviderResult(**result)
        for case, result in zip(scenario.cases, outputs)
    }
    return DeterministicLocalProvider(_identity(name), responses)


def _small_scenario(**thresholds) -> EvaluationScenario:
    return EvaluationScenario(
        "unit", "1",
        (
            ScenarioCase("a", {"id": "a"}, {"answer": 1}, ("unit",), 100),
            ScenarioCase("b", {"id": "b"}, {"answer": 2}, ("unit",), 100),
        ),
        RegressionThresholds(**thresholds),
    )


def test_checked_in_fixture_runs_persists_and_replays_end_to_end(tmp_path) -> None:
    scenario = load_scenario_fixture(SCENARIO_PATH)
    provider = load_provider_fixture(PROVIDER_PATH)
    registry = EvaluationScenarioRegistry()
    assert registry.register(scenario) is scenario
    assert registry.resolve("local-tool-contract", "1.0.0") is scenario
    assert registry.inventory() == ({
        "scenario_id": scenario.scenario_id,
        "version": scenario.version,
        "scenario_digest": scenario.digest,
    },)

    runner = ReproducibleEvaluationRunner()
    first = runner.run(scenario, provider)
    second = runner.run(scenario, load_provider_fixture(PROVIDER_PATH))
    assert first.as_dict() == second.as_dict()
    assert first.assessment.passed
    assert first.pass_rate == 1.0
    assert [item.status for item in first.outcomes] == [OutcomeStatus.PASSED] * 3
    assert runner.replay(first, provider).equivalent

    repository = JsonEvaluationRunRepository(tmp_path / "run.json")
    repository.save(first)
    restored = repository.load()
    assert restored.as_dict() == first.as_dict()
    assert TrajectoryRecord.from_dict(restored.trajectory.as_dict()) == restored.trajectory

    lifecycle_result = first.as_evaluation_result(
        "fixture-run", baseline="baseline@sha", mode=EvaluationMode.OFFLINE,
        replay_equivalent=True,
    )
    assert lifecycle_result.passed
    assert lifecycle_result.metrics == {"error_rate": 0.0, "pass_rate": 1.0, "timeout_rate": 0.0}
    assert lifecycle_result.trajectory_digest == first.trajectory.digest


def test_scenario_registry_rejects_mutation_of_an_existing_version() -> None:
    scenario = _small_scenario()
    registry = EvaluationScenarioRegistry()
    registry.register(scenario)
    changed = replace(scenario, description="changed")
    with pytest.raises(ReproducibleEvaluationError, match="immutable"):
        registry.register(changed)
    with pytest.raises(TypeError):
        scenario.cases[0].input["id"] = "changed"


def test_outcome_taxonomy_is_per_case_and_diagnostics_omit_raw_values() -> None:
    scenario = EvaluationScenario(
        "taxonomy", "1",
        tuple(ScenarioCase(name, {"id": name}, {"ok": True}, (), 100) for name in (
            "assertion", "error", "internal", "invalid", "timeout",
        )),
        RegressionThresholds(min_pass_rate=0, max_timeout_rate=1, max_error_rate=1),
    )
    provider = _provider(scenario, [
        {"kind": "response", "output": {"ok": False}},
        {"kind": "error", "error_code": "unavailable", "error_message": "offline"},
        {"kind": "error", "error_code": "protocol", "error_message": "bad frame"},
        {"kind": "invalid_response", "output": "not-a-response"},
        {"kind": "timeout"},
    ])
    report = ReproducibleEvaluationRunner().run(scenario, provider)
    assert [item.status for item in report.outcomes] == [
        OutcomeStatus.ASSERTION_FAILED,
        OutcomeStatus.PROVIDER_ERROR,
        OutcomeStatus.PROVIDER_ERROR,
        OutcomeStatus.INVALID_RESPONSE,
        OutcomeStatus.TIMEOUT,
    ]
    assert [item.error_category for item in report.outcomes] == [
        ErrorCategory.NONE,
        ErrorCategory.PROVIDER_UNAVAILABLE,
        ErrorCategory.PROVIDER_PROTOCOL,
        ErrorCategory.INVALID_RESPONSE,
        ErrorCategory.DEADLINE_EXCEEDED,
    ]
    diagnostic = evaluation_diagnostics(report)
    assert diagnostic["failed_case_ids"] == [case.case_id for case in scenario.cases]
    assert "input" not in json.dumps(diagnostic)
    assert diagnostic["status_counts"]["timeout"] == 1


def test_regression_gate_catches_case_swap_even_with_allowed_absolute_rate() -> None:
    scenario = _small_scenario(
        min_pass_rate=0.5, max_timeout_rate=0, max_error_rate=0,
        max_pass_rate_drop=0.0, max_case_regressions=0,
    )
    baseline = ReproducibleEvaluationRunner().run(scenario, _provider(scenario, [
        {"kind": "response", "output": {"answer": 1}},
        {"kind": "response", "output": {"answer": 999}},
    ], name="baseline"))
    candidate = ReproducibleEvaluationRunner().run(scenario, _provider(scenario, [
        {"kind": "response", "output": {"answer": 999}},
        {"kind": "response", "output": {"answer": 2}},
    ]), baseline=baseline)
    assert candidate.pass_rate == baseline.pass_rate == 0.5
    assert not candidate.assessment.passed
    assert candidate.assessment.reason_codes == ("maximum_case_regressions",)
    assert candidate.assessment.regressed_case_ids == ("a",)
    assert candidate.assessment.baseline_run_id == baseline.run_id
    assert candidate.assessment.baseline_pass_rate == baseline.pass_rate


def test_matrix_is_identity_sorted_and_rejects_duplicate_targets(tmp_path) -> None:
    scenario = _small_scenario(min_pass_rate=0)
    outputs = [
        {"kind": "response", "output": {"answer": 1}},
        {"kind": "response", "output": {"answer": 2}},
    ]
    provider_b = _provider(scenario, outputs, name="b-model")
    provider_a = _provider(scenario, outputs, name="a-model")
    matrix = ReproducibleEvaluationRunner().run_matrix(scenario, [provider_b, provider_a])
    assert [run.target.model_id for run in matrix.runs] == ["a-model", "b-model"]
    assert matrix.as_dict() == ReproducibleEvaluationRunner().run_matrix(scenario, [provider_a, provider_b]).as_dict()
    repository = JsonEvaluationMatrixRepository(tmp_path / "matrix.json")
    repository.save(matrix)
    assert repository.load() == matrix
    with pytest.raises(ReproducibleEvaluationError, match="unique"):
        ReproducibleEvaluationRunner().run_matrix(scenario, [provider_a, provider_a])


def test_fixture_and_persisted_report_tampering_fail_closed(tmp_path) -> None:
    scenario_payload = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    scenario_payload["cases"][0]["expected_output"] = {"tool": "shell"}
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario_payload), encoding="utf-8")
    with pytest.raises(ReproducibleEvaluationError, match="digest mismatch"):
        load_scenario_fixture(scenario_path)

    provider_payload = json.loads(PROVIDER_PATH.read_text(encoding="utf-8"))
    provider_payload["responses"][0]["output"] = {"tool": "shell"}
    provider_path = tmp_path / "provider.json"
    provider_path.write_text(json.dumps(provider_payload), encoding="utf-8")
    with pytest.raises(ReproducibleEvaluationError, match="digest mismatch"):
        load_provider_fixture(provider_path)

    report = ReproducibleEvaluationRunner().run(load_scenario_fixture(SCENARIO_PATH), load_provider_fixture(PROVIDER_PATH))
    repository = JsonEvaluationRunRepository(tmp_path / "run.json")
    repository.save(report)
    persisted = json.loads(repository.path.read_text(encoding="utf-8"))
    persisted["outcomes"][0]["status"] = "assertion_failed"
    repository.path.write_text(json.dumps(persisted), encoding="utf-8")
    with pytest.raises(ReproducibleEvaluationError, match="summary mismatch|digest mismatch"):
        repository.load()


def test_replay_refuses_identity_drift() -> None:
    scenario = _small_scenario(min_pass_rate=0)
    outputs = [
        {"kind": "response", "output": {"answer": 1}},
        {"kind": "response", "output": {"answer": 2}},
    ]
    runner = ReproducibleEvaluationRunner()
    report = runner.run(scenario, _provider(scenario, outputs, name="baseline"))
    with pytest.raises(ReproducibleEvaluationError, match="identity"):
        runner.replay(report, _provider(scenario, outputs, name="candidate"))


def test_cli_executes_checked_in_matrix_fixture(tmp_path, capsys) -> None:
    module_path = ROOT / "scripts" / "run_reproducible_eval.py"
    spec = importlib.util.spec_from_file_location("run_reproducible_eval", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_reproducible_eval"] = module
    spec.loader.exec_module(module)
    output = tmp_path / "matrix.json"
    assert module.main([
        "--scenario", str(SCENARIO_PATH), "--provider", str(PROVIDER_PATH), "--output", str(output),
    ]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["runs"][0]["gate_passed"] is True
    assert summary["privacy"] == "report contains raw replay inputs and outputs"
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["matrix_id"] == summary["matrix_id"]

    compared = tmp_path / "compared.json"
    assert module.main([
        "--scenario", str(SCENARIO_PATH), "--provider", str(PROVIDER_PATH),
        "--baseline", str(output), "--output", str(compared),
    ]) == 0
    compared_summary = json.loads(capsys.readouterr().out)
    assert compared_summary["runs"][0]["regressed_case_ids"] == []

    assert module.main([
        "--scenario", str(SCENARIO_PATH), "--provider", str(PROVIDER_PATH), "--output", str(SCENARIO_PATH),
    ]) == 2
    assert "must not overwrite" in capsys.readouterr().err
