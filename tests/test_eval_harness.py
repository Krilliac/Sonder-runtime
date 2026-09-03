"""Unit coverage for eval_harness: registry, providers, runner, baseline.

Everything here is offline. Providers are CallableProvider/ReplayProvider
stubs; grading uses the real grounding.run_code subprocess path only where
the test is specifically about execution outcomes, so the suite stays fast.
"""
import json
import os
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


# --- chunked / focused selection ---------------------------------------------

def _multi_suite(tmp_path):
    return eh.load_suite(_suite_file(tmp_path, scenarios=[
        {"id": name, "prompt": "p " + name, "check": "assert True"}
        for name in ("a", "b", "c", "d")]))


def test_select_scenarios_full_selection_is_identity(tmp_path):
    suite = _multi_suite(tmp_path)
    assert eh.select_scenarios(suite) is suite
    assert eh.select_scenarios(suite, start=0, count=4) is suite


def test_select_scenarios_chunk_recomputes_hash(tmp_path):
    suite = _multi_suite(tmp_path)
    chunk = eh.select_scenarios(suite, start=1, count=2)
    assert [s["id"] for s in chunk["scenarios"]] == ["b", "c"]
    assert chunk["suite_hash"] != suite["suite_hash"]
    assert chunk["selection"] == {"only": None, "start": 1, "count": 2}


def test_select_scenarios_only_filter(tmp_path):
    suite = _multi_suite(tmp_path)
    narrowed = eh.select_scenarios(suite, only=["d", "a"])
    assert [s["id"] for s in narrowed["scenarios"]] == ["a", "d"]
    assert narrowed["selection"]["only"] == ["a", "d"]


@pytest.mark.parametrize("kwargs", [
    {"only": ["nope"]},
    {"start": -1},
    {"count": 0},
    {"start": 99},
])
def test_select_scenarios_rejects_bad_selection(tmp_path, kwargs):
    with pytest.raises(eh.HarnessError):
        eh.select_scenarios(_multi_suite(tmp_path), **kwargs)


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
                      "timeout": 0, "verifier_unavailable": 0, "unknown": 0,
                      "abandoned": 0, "graded": 1, "infra": 1, "pass_rate": 1.0,
                      "trials": 1, "pass_at_k": 1, "cassette_drift": 0}


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


# --- verifiers and the outcome classes they add ------------------------------

def test_a_scenario_may_name_a_registered_verifier_and_it_changes_the_hash():
    import verifiers
    plain = _scenario()
    verified = _scenario(verifier="python_exec")
    assert plain["verifier"] is None and verified["verifier"] == "python_exec"
    assert eh._hashed_view(plain) == {
        key: plain[key]
        for key in ("id", "kind", "prompt", "check", "timeout_s", "max_attempts")}
    assert eh._hashed_view(verified)["verifier"] == "python_exec"
    assert "python_exec" in verifiers.REGISTRY
    with pytest.raises(eh.HarnessError):
        _scenario(verifier="no_such_verifier")
    with pytest.raises(eh.HarnessError):
        _scenario(verifier_spec=["not", "an", "object"])


def test_the_python_exec_verifier_routes_through_the_injected_grader():
    case = eh.run_case(_scenario(verifier="python_exec"),
                       eh.CallableProvider(lambda prompt: GOOD),
                       run_code_fn=_fake_run_code)
    assert case["status"] == "pass"
    assert [event["event"] for event in case["events"]] == ["generate", "exec", "outcome"]


def test_a_verifier_that_cannot_judge_is_its_own_status(monkeypatch):
    import verifiers

    def unavailable(artifact, spec=None):
        raise verifiers.VerifierUnavailable("mypy is not installed")

    monkeypatch.setitem(verifiers.REGISTRY, "fake_unavailable", unavailable)
    case = eh.run_case(_scenario(verifier="fake_unavailable"),
                       eh.CallableProvider(lambda prompt: GOOD),
                       run_code_fn=_fake_run_code)
    assert case["status"] == "verifier_unavailable"
    assert case["failure"]["kind"] == "verifier_unavailable"
    assert "mypy" in case["failure"]["message"]
    assert case["passed"] is False


def test_a_named_verifier_that_passes_is_recorded_as_a_verify_event(monkeypatch):
    import verifiers
    seen = []

    def judge(artifact, spec=None):
        seen.append(spec["check"])
        return verifiers.Verdict(True, "passed", "all good")

    monkeypatch.setitem(verifiers.REGISTRY, "fake_judge", judge)
    case = eh.run_case(_scenario(verifier="fake_judge", verifier_spec={"threshold": 7}),
                       eh.CallableProvider(lambda prompt: GOOD),
                       run_code_fn=_fake_run_code)
    assert case["status"] == "pass"
    verify = next(event for event in case["events"] if event["event"] == "verify")
    assert verify["verifier"] == "fake_judge" and verify["ok"] is True
    assert seen == ["assert double(2) == 4"]
    assert case["trajectory"]["steps"][0]["output"]["exec_ok"] is True


def test_a_harness_crash_is_unknown_never_a_graded_zero(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr(eh.solver, "solve", boom)
    case = eh.run_case(_scenario(), eh.CallableProvider(lambda prompt: GOOD),
                       run_code_fn=_fake_run_code)
    assert case["status"] == "unknown"
    assert case["failure"]["kind"] == "harness_crash"
    assert "solver exploded" in case["failure"]["message"]


def test_a_live_provider_that_ran_out_of_wall_clock_after_an_attempt_is_abandoned():
    def slow(prompt):
        time.sleep(1.2)
        return GOOD

    live = eh.CallableProvider(slow, name="live", deterministic=False)
    case = eh.run_case(_scenario(), live, run_code_fn=_fake_run_code,
                       case_timeout=0.3)
    assert case["status"] == "abandoned"
    assert case["failure"]["kind"] == "abandoned"
    assert case["attempts"] == 1


def test_a_provider_error_escaping_solve_verified_is_still_a_provider_error(monkeypatch):
    import verifiers
    monkeypatch.setitem(verifiers.REGISTRY, "fake_judge",
                        lambda artifact, spec=None: verifiers.Verdict(True, "ok", ""))

    def down(prompt):
        raise RuntimeError("provider down")

    case = eh.run_case(_scenario(verifier="fake_judge"), eh.CallableProvider(down),
                       run_code_fn=_fake_run_code)
    assert case["status"] == "error"
    assert case["failure"]["kind"] == "provider_error"


# --- trials ------------------------------------------------------------------

def test_trials_report_pass_at_1_and_pass_at_k_separately(tmp_path):
    suite = eh.load_suite(_suite_file(tmp_path, scenarios=[
        {"id": "double", "prompt": "p", "check": "assert double(2) == 4",
         "max_attempts": 1}]))
    responses = iter([BUGGY, GOOD, GOOD])
    provider = eh.CallableProvider(lambda prompt: next(responses),
                                   deterministic=False)
    summary = eh.run_suite(suite, provider, run_code_fn=_fake_run_code, trials=3)
    case = summary["cases"][0]
    assert case["status"] == "fail"
    assert case["trials"] == ["fail", "pass", "pass"]
    assert case["pass_at_k"] is True
    assert summary["totals"]["pass"] == 0 and summary["totals"]["pass_at_k"] == 1
    assert summary["totals"]["trials"] == 3 and summary["trials"] == 3


@pytest.mark.parametrize("trials", [0, 11, "3"])
def test_trials_are_bounded(tmp_path, trials):
    suite = eh.load_suite(_suite_file(tmp_path))
    with pytest.raises(eh.HarnessError):
        eh.run_suite(suite, eh.CallableProvider(lambda prompt: GOOD),
                     run_code_fn=_fake_run_code, trials=trials)


def test_totals_carry_the_full_outcome_vocabulary(tmp_path):
    summary = _summary(tmp_path)
    assert set(summary["totals"]) == {
        "cases", "pass", "fail", "error", "timeout", "verifier_unavailable",
        "unknown", "abandoned", "graded", "infra", "pass_rate", "trials",
        "pass_at_k", "cassette_drift"}
    assert summary["harness_version"] == 2


# --- history policy ----------------------------------------------------------

def test_history_is_recorded_by_default_only_where_honest(tmp_path):
    summary = _summary(tmp_path)
    summary["provider"]["digest"] = "unavailable: boom"
    record, note = eh.history_disposition(summary)
    assert record is False and "--record-history" in note
    summary["provider"]["digest"] = "a" * 64
    assert eh.history_disposition(summary)[0] is True
    assert eh.history_disposition(summary, requested=False)[0] is False
    assert eh.history_disposition(summary, requested=True)[0] is True
    summary["provider"]["digest"] = "unavailable: boom"
    assert eh.history_disposition(summary, requested=True)[0] is True, (
        "an explicit request is refused by record_history, never skipped")


def test_a_vacuous_run_is_not_recorded_by_default(tmp_path):
    def explode(prompt):
        raise RuntimeError("down")

    summary = _summary(tmp_path, provider=eh.CallableProvider(explode))
    summary["provider"]["digest"] = "a" * 64
    record, note = eh.history_disposition(summary)
    assert record is False and "zero graded" in note


# --- comparing two runs ------------------------------------------------------

def _run_dir(tmp_path, name, response):
    suite = eh.load_suite(_suite_file(tmp_path))
    out = str(tmp_path / name)
    eh.run_suite(suite, eh.CallableProvider(lambda prompt: response), out_dir=out,
                 run_code_fn=_fake_run_code)
    return out


def test_compare_names_exactly_the_doctored_case(tmp_path):
    before = _run_dir(tmp_path, "before", GOOD)
    after = _run_dir(tmp_path, "after", BUGGY)
    comparison = eh.compare_runs(before, after, provider="callable")
    assert comparison["regressed"] == ["double"]
    assert comparison["passed"] is False
    assert "case_regressions" in comparison["reason_codes"]
    assert comparison["cases"][0]["trajectory"] == "divergent"
    assert comparison["cases"][0]["divergences"], "the trace records give a step-level answer"
    assert comparison["assessment"]["regressed_case_ids"] == ["double"]
    assert comparison["assessment"]["baseline_run_id"] == comparison["before"]["report_id"]


def test_compare_of_a_run_with_itself_is_clean(tmp_path):
    before = _run_dir(tmp_path, "before", GOOD)
    comparison = eh.compare_runs(before, before, provider="callable")
    assert comparison["passed"] is True
    assert comparison["reason_codes"] == []
    assert comparison["cases"][0]["trajectory"] == "same"


def test_compare_cli_writes_the_comparison_and_exits_one_on_regression(tmp_path):
    before = _run_dir(tmp_path, "before", GOOD)
    after = _run_dir(tmp_path, "after", BUGGY)
    exit_code = eh.main(["compare", "--run", before, "--run", after,
                         "--provider", "callable"])
    assert exit_code == 1
    with open(os.path.join(after, "comparison.json"), encoding="utf-8") as handle:
        written = json.load(handle)
    assert written["schema"] == eh.COMPARISON_SCHEMA
    assert written["regressed"] == ["double"]
    assert eh.main(["compare", "--run", before, "--run", before, "--provider",
                    "callable", "--out", str(tmp_path / "same.json")]) == 0
    assert eh.main(["compare", "--run", before, "--provider", "callable"]) == 2


def test_load_run_summary_refuses_a_missing_or_foreign_run(tmp_path):
    with pytest.raises(eh.HarnessError):
        eh.load_run_summary(str(tmp_path / "nope"), "callable")
    bogus = tmp_path / "bogus" / "callable"
    bogus.mkdir(parents=True)
    (bogus / "summary.json").write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
    with pytest.raises(eh.HarnessError):
        eh.load_run_summary(str(tmp_path / "bogus"), "callable")
