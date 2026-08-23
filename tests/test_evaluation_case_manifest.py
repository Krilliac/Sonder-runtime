from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from sonder_runtime.application.evaluation import (
    EvaluationCase,
    EvaluationCaseGrader,
    EvaluationCaseManifest,
    EvaluationCaseManifestError,
    EvaluationCaseProvenance,
    inspect_manifest,
    load_manifest,
)
from sonder_runtime.application.evaluation.case_manifest import MAX_CASE_BYTES, SCHEMA
from sonder_runtime.application.evaluation.proposal_lifecycle import EvaluationDimension, EvaluationSuite


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "docs" / "research" / "examples" / "evaluation-case-manifest.json"
SCHEMA_PATH = ROOT / "docs" / "research" / "schemas" / "evaluation-case-manifest.schema.json"
CHECKER = ROOT / "scripts" / "check_eval_manifest.py"


def _suite() -> EvaluationSuite:
    return EvaluationSuite(
        "golden", "1", (EvaluationDimension("split", "holdout"),), ("passed",),
    )


def _case(
    case_id: str = "case-1", *, verifier: str = "python_exec", advisory: bool = False,
    input_value="implement f", target=None,
) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        input=input_value,
        target={"expected": 1} if target is None else target,
        grader=EvaluationCaseGrader(verifier, {"check": "assert f() == 1"}, advisory),
        provenance=EvaluationCaseProvenance("tests", "fixture-v1"),
        tags=("holdout", "python"),
    )


def _manifest(*cases: EvaluationCase) -> EvaluationCaseManifest:
    return EvaluationCaseManifest(_suite(), tuple(cases or (_case(),)))


def test_manifest_round_trip_is_immutable_and_digest_stable() -> None:
    manifest = _manifest(_case("case-1"), _case("case-2"))
    payload = manifest.as_dict()
    restored = EvaluationCaseManifest.from_dict(json.loads(json.dumps(payload)))

    assert restored == manifest
    assert restored.digest == manifest.digest
    assert payload["manifest_digest"] == manifest.digest
    assert payload["cases"][0]["case_digest"] == manifest.cases[0].digest

    # Canonical identity ignores JSON object key order, while nested input/spec
    # values cannot be mutated through a frozen dataclass.
    reordered = json.loads(json.dumps(payload, sort_keys=False))
    reordered["cases"][0]["grader"]["spec"] = {
        key: reordered["cases"][0]["grader"]["spec"][key]
        for key in reversed(reordered["cases"][0]["grader"]["spec"])
    }
    assert EvaluationCaseManifest.from_dict(reordered).digest == manifest.digest
    with pytest.raises(TypeError):
        restored.cases[0].grader.spec["new"] = "value"


def test_manifest_binds_suite_case_and_manifest_digests() -> None:
    payload = _manifest().as_dict()

    suite_tamper = json.loads(json.dumps(payload))
    suite_tamper["suite"]["version"] = "2"
    with pytest.raises(EvaluationCaseManifestError, match="suite digest mismatch"):
        EvaluationCaseManifest.from_dict(suite_tamper)

    case_tamper = json.loads(json.dumps(payload))
    case_tamper["cases"][0]["target"] = {"expected": 2}
    with pytest.raises(EvaluationCaseManifestError, match="case digest mismatch"):
        EvaluationCaseManifest.from_dict(case_tamper)

    manifest_tamper = json.loads(json.dumps(payload))
    manifest_tamper["manifest_digest"] = "0" * 64
    with pytest.raises(EvaluationCaseManifestError, match="manifest digest mismatch"):
        EvaluationCaseManifest.from_dict(manifest_tamper)


def test_manifest_rejects_ambiguous_order_duplicates_and_unknown_fields() -> None:
    with pytest.raises(EvaluationCaseManifestError, match="unique and sorted"):
        _manifest(_case("z"), _case("a"))
    with pytest.raises(EvaluationCaseManifestError, match="unique and sorted"):
        _manifest(_case("same"), _case("same"))

    payload = _manifest().as_dict()
    payload["surprise"] = True
    with pytest.raises(EvaluationCaseManifestError, match="unknown fields: surprise"):
        EvaluationCaseManifest.from_dict(payload)

    dimension_tamper = _manifest().as_dict()
    dimension_tamper["suite"]["dimensions"][0]["extra"] = "ambiguous"
    with pytest.raises(EvaluationCaseManifestError, match="suite dimension has unknown fields"):
        EvaluationCaseManifest.from_dict(dimension_tamper)


def test_json_contract_is_bounded_and_rejects_non_finite_or_executable_values() -> None:
    with pytest.raises(EvaluationCaseManifestError, match="non-finite"):
        _case(input_value={"temperature": float("nan")})
    with pytest.raises(EvaluationCaseManifestError, match="JSON-compatible"):
        _case(input_value={"callback": lambda: None})
    with pytest.raises(EvaluationCaseManifestError, match="case exceeds"):
        _case(input_value="x" * MAX_CASE_BYTES)

    nested: object = "leaf"
    for _ in range(30):
        nested = {"next": nested}
    with pytest.raises(EvaluationCaseManifestError, match="nesting bound"):
        _case(input_value=nested)


def test_model_grader_is_structurally_advisory_only() -> None:
    with pytest.raises(EvaluationCaseManifestError, match="model-graded verifiers must be advisory"):
        _case(verifier="llm_judge", advisory=False)

    advisory = _case(verifier="llm_judge", advisory=True)
    diagnostics = inspect_manifest(_manifest(advisory), {"llm_judge"})
    assert diagnostics.runnable
    assert not diagnostics.gate_ready
    assert diagnostics.warnings == ("manifest has no deterministic promotion-gate cases",)


def test_preflight_distinguishes_missing_gate_and_advisory_verifiers_without_content() -> None:
    manifest = _manifest(
        _case("a", verifier="missing_gate", input_value="PRIVATE-PROMPT"),
        _case("b", verifier="missing_advice", advisory=True),
    )
    diagnostics = inspect_manifest(manifest, {"python_exec"})
    payload = diagnostics.as_dict()

    assert not diagnostics.runnable
    assert not diagnostics.gate_ready
    assert payload["unavailable_deterministic"] == ["missing_gate"]
    assert payload["unavailable_advisory"] == ["missing_advice"]
    assert "PRIVATE-PROMPT" not in json.dumps(payload)
    assert set(payload) == {
        "schema", "manifest_digest", "case_count", "deterministic_cases",
        "advisory_cases", "unavailable_deterministic", "unavailable_advisory",
        "runnable", "gate_ready", "warnings",
    }


def test_loader_is_local_bounded_and_does_not_echo_invalid_content(tmp_path) -> None:
    payload = _manifest().as_dict()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_manifest(path).digest == _manifest().digest

    secret = tmp_path / "secret.json"
    secret.write_text('{"PRIVATE-TOKEN":', encoding="utf-8")
    with pytest.raises(EvaluationCaseManifestError, match="unreadable") as caught:
        load_manifest(secret)
    assert "PRIVATE-TOKEN" not in str(caught.value)

    oversized = tmp_path / "large.json"
    oversized.write_bytes(b" " * 100)
    with pytest.raises(EvaluationCaseManifestError, match="byte bound"):
        load_manifest(oversized, max_bytes=10)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema":"first","schema":"PRIVATE-DUPLICATE"}', encoding="utf-8")
    with pytest.raises(EvaluationCaseManifestError, match="duplicate object fields") as duplicate_error:
        load_manifest(duplicate)
    assert "PRIVATE-DUPLICATE" not in str(duplicate_error.value)


def test_documented_schema_and_example_track_the_python_contract() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == SCHEMA
    assert schema["properties"]["cases"]["maxItems"] == 256
    model_grader_rule = schema["$defs"]["case"]["allOf"][0]
    assert model_grader_rule["then"]["properties"]["grader"]["properties"]["advisory"]["const"] is True

    example = load_manifest(EXAMPLE)
    assert [case.case_id for case in example.cases] == ["json-shape", "python-reverse"]
    assert inspect_manifest(example, {"json_schema", "python_exec"}).gate_ready


def test_checker_reports_ready_json_without_case_payloads() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHECKER), str(EXAMPLE), "--json"],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["valid"] is True
    assert payload["runnable"] is True
    assert payload["gate_ready"] is True
    assert payload["case_count"] == 2
    assert "Implement reverse_string" not in completed.stdout


def test_checker_fails_closed_and_redacts_invalid_file(tmp_path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"secret":"DO-NOT-ECHO"}', encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(CHECKER), str(invalid), "--json"],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["valid"] is False
    assert "DO-NOT-ECHO" not in completed.stdout


def test_checker_returns_not_ready_for_unknown_deterministic_verifier(tmp_path) -> None:
    path = tmp_path / "unknown-verifier.json"
    manifest = _manifest(_case(verifier="not_installed", input_value="PRIVATE-CASE"))
    path.write_text(json.dumps(manifest.as_dict()), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(CHECKER), str(path), "--json"],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["valid"] is True
    assert payload["runnable"] is False
    assert payload["gate_ready"] is False
    assert payload["unavailable_deterministic"] == ["not_installed"]
    assert "PRIVATE-CASE" not in completed.stdout
