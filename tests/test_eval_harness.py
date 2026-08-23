"""Unit coverage for eval_harness: registry, providers, runner, baseline.

Everything here is offline. Providers are CallableProvider/ReplayProvider
stubs; grading uses the real grounding.run_code subprocess path only where
the test is specifically about execution outcomes, so the suite stays fast.
"""
import json
import time

import pytest

import eval_harness as eh


# --- helpers -----------------------------------------------------------------

def _suite_file(tmp_path, **overrides):
    payload = {
        "schema": eh.SUITE_SCHEMA,
        "suite": "unit",
        "version": 1,
        "scenarios": [
            {"id": "double",
             "prompt": "Write a Python function named `double(x)` that "
                       "returns x * 2. Return ONLY the function in one "
                       "python code block.",
             "check": "assert double(2) == 4\nassert double(0) == 0",
             "max_attempts": 2},
        ],
    }
    payload.update(overrides)
    path = tmp_path / "unit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _scenario(**overrides):
    raw = {"id": "double",
           "prompt": "Write a Python function named `double(x)` that returns "
                     "x * 2. Return ONLY the function in one python code "
                     "block.",
           "check": "assert double(2) == 4",
           "max_attempts": 2}
    raw.update(overrides)
    return eh.normalize_scenario(raw, source="test")


GOOD = "```python\ndef double(x):\n    return x * 2\n```"
BUGGY = "```python\ndef double(x):\n    return x * 3\n```"
NO_CODE = "Here is my answer without any code block."


def _fake_run_code(code, check, timeout=8):
    """In-process grader: no subprocess, so unit tests stay instant."""
    namespace = {}
    try:
        exec(code + "\n" + check, namespace)  # trusted literals from this file
    except AssertionError as exc:
        return False, "AssertionError: %s" % exc
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    return True, ""


# --- suite registry ----------------------------------------------------------

def test_load_suite_resolves_and_hashes(tmp_path):
    suite = eh.load_suite(_suite_file(tmp_path))
    assert suite["suite"] == "unit"
    assert [s["id"] for s in suite["scenarios"]] == ["double"]
    assert len(suite["suite_hash"]) == 64


def test_suite_hash_tracks_check_content_not_tags(tmp_path):
    base = eh.load_suite(_suite_file(tmp_path))
    retagged = eh.load_suite(_suite_file(
        tmp_path, scenarios=[dict(base["scenarios"][0],
                                  tags=["extra"], source=None)]))
    assert retagged["suite_hash"] == base["suite_hash"]
    changed = eh.load_suite(_suite_file(
        tmp_path, scenarios=[dict(base["scenarios"][0],
                                  check="assert double(3) == 6",
                                  source=None)]))
    assert changed["suite_hash"] != base["suite_hash"]


@pytest.mark.parametrize("overrides", [
    {"schema": "wrong"},
    {"version": "1"},
    {"scenarios": []},
    {"scenarios": [{"id": "a", "prompt": "p", "check": "c"},
                   {"id": "a", "prompt": "p", "check": "c"}]},
    {"scenarios": [{"id": "a", "prompt": "p", "check": "c",
                    "kind": "made_up"}]},
    {"scenarios": [{"id": "a", "prompt": "", "check": "c"}]},
    {"scenarios": [{"id": "a", "prompt": "p", "check": "c",
                    "max_attempts": 0}]},
    {"builtin_tasks": ["no_such_training_task"]},
])
def test_load_suite_rejects_invalid(tmp_path, overrides):
    with pytest.raises(eh.HarnessError):
        eh.load_suite(_suite_file(tmp_path, **overrides))


def test_builtin_tasks_adapt_training_tasks(tmp_path):
    suite = eh.load_suite(_suite_file(tmp_path,
                                      builtin_tasks=["reverse_string"]))
    ids = [s["id"] for s in suite["scenarios"]]
    assert "reverse_string" in ids
    builtin = next(s for s in suite["scenarios"]
                   if s["id"] == "reverse_string")
    assert builtin["source"] == "builtin:training_tasks"
    assert "reverse_string" in builtin["check"]


def test_discover_suites_skips_baseline_and_rejects_duplicates(tmp_path):
    _suite_file(tmp_path)
    (tmp_path / "eval_baseline.json").write_text("{}", encoding="utf-8")
    assert list(eh.discover_suites(str(tmp_path))) == ["unit"]
    (tmp_path / "unit2.json").write_text(
        (tmp_path / "unit.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    with pytest.raises(eh.HarnessError):
        eh.discover_suites(str(tmp_path))


# --- replay provider ---------------------------------------------------------

def _cassette(entries):
    return {"schema": eh.CASSETTE_SCHEMA, "suite": "unit",
            "recorded_from": "manual", "entries": entries}


def test_replay_provider_serves_in_order_and_misses_loudly():
    provider = eh.ReplayProvider(_cassette({"double": [
        {"response": "first"}, {"response": "second"}]}))
    provider.begin_case("double")
    assert provider.generate("p1") == "first"
    assert provider.generate("p2") == "second"
    with pytest.raises(eh.CassetteMiss):
        provider.generate("p3")


def test_replay_provider_counts_prompt_drift():
    entry = {"response": "r", "prompt_sha256": eh._sha256("recorded prompt")}
    provider = eh.ReplayProvider(_cassette({"double": [entry]}))
    provider.begin_case("double")
    provider.generate("a different prompt")
    assert provider.drift == [("double", 0)]


def test_replay_provider_rejects_malformed_cassette():
    with pytest.raises(eh.HarnessError):
        eh.ReplayProvider({"schema": "wrong", "entries": {}})
    with pytest.raises(eh.HarnessError):
        eh.ReplayProvider(_cassette(None))


# --- case runner outcome classes --------------------------------------------

def test_case_pass_first_attempt():
    provider = eh.CallableProvider(lambda prompt: GOOD)
    case = eh.run_case(_scenario(), provider, run_code_fn=_fake_run_code)
    assert case["status"] == "pass"
    assert case["attempts"] == 1
    assert case["failure"] is None


def test_case_repair_pass_second_attempt():
    responses = iter([BUGGY, GOOD])
    provider = eh.CallableProvider(lambda prompt: next(responses))
    case = eh.run_case(_scenario(), provider, run_code_fn=_fake_run_code)
    assert case["status"] == "pass"
    assert case["attempts"] == 2
    generates = [e for e in case["events"] if e["event"] == "generate"]
    assert len(generates) == 2
    # the second prompt is a repair prompt built from the first failure
    assert "BUG" in generates[1]["prompt"]


def test_case_graded_failure_is_assertion_kind():
    provider = eh.CallableProvider(lambda prompt: BUGGY)
    case = eh.run_case(_scenario(), provider, run_code_fn=_fake_run_code)
    assert case["status"] == "fail"
    assert case["failure"]["kind"] == "assertion"


def test_case_no_code_block_kind():
    provider = eh.CallableProvider(lambda prompt: NO_CODE)
    case = eh.run_case(_scenario(), provider, run_code_fn=_fake_run_code)
    assert case["status"] == "fail"
    assert case["failure"]["kind"] == "no_code"


def test_case_provider_error_is_not_a_graded_fail():
    def explode(prompt):
        raise RuntimeError("provider down")
    case = eh.run_case(_scenario(), eh.CallableProvider(explode),
                       run_code_fn=_fake_run_code)
    assert case["status"] == "error"
    assert case["failure"]["kind"] == "provider_error"


def test_case_cassette_miss_is_error_status():
    provider = eh.ReplayProvider(_cassette({"other": []}))
    case = eh.run_case(_scenario(), provider, run_code_fn=_fake_run_code)
    assert case["status"] == "error"
    assert case["failure"]["kind"] == "cassette_miss"


def test_case_wall_clock_timeout():
    def slow(prompt):
        time.sleep(1.2)
        return GOOD
    case = eh.run_case(_scenario(), eh.CallableProvider(slow),
                       run_code_fn=_fake_run_code, case_timeout=0.3)
    assert case["status"] == "timeout"
    assert case["failure"]["kind"] == "case_timeout"


def test_case_exec_timeout_kind():
    hang = "```python\ndef double(x):\n    while True:\n        pass\n```"
    scenario = _scenario(timeout_s=1, max_attempts=1)
    case = eh.run_case(scenario, eh.CallableProvider(lambda p: hang))
    assert case["status"] == "fail"
    assert case["failure"]["kind"] == "exec_timeout"


def test_trajectory_digest_is_deterministic():
    provider = eh.CallableProvider(lambda prompt: GOOD)
    first = eh.run_case(_scenario(), provider, run_code_fn=_fake_run_code)
    second = eh.run_case(_scenario(), provider, run_code_fn=_fake_run_code)
    assert (first["trajectory"]["trajectory_digest"]
            == second["trajectory"]["trajectory_digest"])


# --- totals discipline -------------------------------------------------------

def test_pass_rate_excludes_infrastructure_failures(tmp_path):
    suite = eh.load_suite(_suite_file(tmp_path, scenarios=[
        {"id": "ok", "prompt": "p ok", "check": "assert double(2) == 4",
         "max_attempts": 1},
        {"id": "missing", "prompt": "p missing", "check": "assert True",
         "max_attempts": 1},
    ]))
    # cassette covers only one scenario; the other becomes an infra error
    provider = eh.ReplayProvider(_cassette({"ok": [{"response": GOOD}]}))
    summary = eh.run_suite(suite, provider, run_code_fn=_fake_run_code)
    totals = summary["totals"]
    assert totals == {"cases": 2, "pass": 1, "fail": 0, "error": 1,
                      "timeout": 0, "graded": 1, "pass_rate": 1.0,
                      "cassette_drift": 0}


# --- baseline ratchet --------------------------------------------------------

def _summary(tmp_path, provider=None):
    suite = eh.load_suite(_suite_file(tmp_path))
    provider = provider or eh.CallableProvider(lambda prompt: GOOD)
    return eh.run_suite(suite, provider, run_code_fn=_fake_run_code)


def _baseline_for(summary, **entry_overrides):
    entry = {"min_pass_rate": 1.0,
             "required_pass": [c["scenario"] for c in summary["cases"]],
             "forbid_infra": True,
             "suite_hash": summary["suite_hash"]}
    entry.update(entry_overrides)
    return {"schema": eh.BASELINE_SCHEMA,
            "suites": {summary["suite"]:
                       {summary["provider"]["name"]: entry}}}


def test_baseline_green_run_has_no_violations(tmp_path):
    summary = _summary(tmp_path)
    assert eh.check_baseline(summary, _baseline_for(summary)) == []


def test_baseline_missing_entry_is_itself_a_violation(tmp_path):
    summary = _summary(tmp_path)
    violations = eh.check_baseline(
        summary, {"schema": eh.BASELINE_SCHEMA, "suites": {}})
    assert len(violations) == 1
    assert "no baseline entry" in violations[0]


def test_baseline_detects_suite_hash_change(tmp_path):
    summary = _summary(tmp_path)
    baseline = _baseline_for(summary, suite_hash="f" * 64)
    assert any(v.startswith("suite_changed")
               for v in eh.check_baseline(summary, baseline))


def test_baseline_detects_pass_rate_and_required_pass_regression(tmp_path):
    summary = _summary(tmp_path,
                       provider=eh.CallableProvider(lambda prompt: BUGGY))
    violations = eh.check_baseline(summary, _baseline_for(summary))
    kinds = {v.split(":")[0] for v in violations}
    assert "pass_rate_regression" in kinds
    assert "required_pass" in kinds


def test_baseline_forbids_infrastructure_failures(tmp_path):
    def explode(prompt):
        raise RuntimeError("down")
    summary = _summary(tmp_path, provider=eh.CallableProvider(explode))
    violations = eh.check_baseline(summary, _baseline_for(summary))
    assert any(v.startswith("infra_failure") for v in violations)


def test_update_baseline_records_current_outcomes(tmp_path):
    summary = _summary(tmp_path)
    updated = eh.update_baseline(
        {"schema": eh.BASELINE_SCHEMA, "suites": {}}, summary)
    entry = updated["suites"]["unit"][summary["provider"]["name"]]
    assert entry["required_pass"] == ["double"]
    assert entry["min_pass_rate"] == 1.0
    assert entry["suite_hash"] == summary["suite_hash"]
    # the updated baseline immediately holds against the same run
    assert eh.check_baseline(summary, updated) == []


# --- report + failures payload ----------------------------------------------

def test_report_lists_failures_with_trace_pointer(tmp_path):
    summary = _summary(tmp_path,
                       provider=eh.CallableProvider(lambda prompt: BUGGY))
    report = eh.render_report([summary], ["pass_rate_regression: fake"])
    assert "## Failures" in report
    assert "double" in report
    assert "traces/double.jsonl" in report
    assert "Baseline violations" in report
    payload = eh.failures_json([summary])
    assert payload["failures"][0]["scenario"] == "double"
    assert payload["failures"][0]["status"] == "fail"
    assert payload["failures"][0]["failure"]["kind"] == "assertion"


# --- durable history integration ---------------------------------------------

def test_record_history_appends_identity_bound_record(tmp_path):
    import sonder_runtime.adapters.evaluation_history_store as store
    summary = _summary(tmp_path)
    summary["provider"]["digest"] = "a" * 64
    history_path = tmp_path / "history.jsonl"
    record = eh.record_history(summary, str(history_path))
    assert record["identity"]["suite"] == "eval-harness:unit"
    assert record["result"] == {"passed": 1, "total": 1, "pass_rate": 1.0}
    loaded = store.load_history(str(history_path))
    assert len(loaded["records"]) == 1
    assert loaded["malformed"] == 0


def test_record_history_refuses_non_digest_provider(tmp_path):
    summary = _summary(tmp_path)
    summary["provider"]["digest"] = "unavailable: boom"
    with pytest.raises(eh.HarnessError):
        eh.record_history(summary, str(tmp_path / "history.jsonl"))


def test_record_history_refuses_vacuous_run(tmp_path):
    def explode(prompt):
        raise RuntimeError("down")
    summary = _summary(tmp_path, provider=eh.CallableProvider(explode))
    summary["provider"]["digest"] = "a" * 64
    with pytest.raises(eh.HarnessError):
        eh.record_history(summary, str(tmp_path / "history.jsonl"))
