"""Did the schema actually help -- and can this harness be trusted to say so?

Plan 03's last task is the one that decides whether the plan achieved anything,
and it is also the one most able to produce a dishonest number. The recorded
incident this repository is guarding against is a count reported as an
improvement four times over ("106 -> 14 -> 2 errors") when the real counts were
99 and then 109: each fall meant the toolchain stopped earlier, not that the
code got better.

A sibling in this very repository still has the shape. ``benchmark_moat.run_arm``
scores a model call that never happened as ``0.0``, and its arm aggregate carries
no count of those, so one outage silently drags a pass rate down and is
indistinguishable from a model that answered badly.

So these tests spend most of their weight on one property: three things that are
NEVER the same thing.

* a call that **ran and produced valid output**
* a call that **ran and was rejected** -- schema violation, or a quote that is
  not in the source
* a call that **never ran** -- model unavailable, timeout, load failure

The third must not enter a rate's numerator *or* its denominator, must be
counted and printed anyway, and must make a comparison refuse itself when the
two arms did not reach the same stage. An arm with fewer completions is not a
better arm, however good its surviving rate looks.

The rest pin two things Task 2 explicitly deferred here: that a rejection is
classified into *reflow* versus *not in the source*, so a rejection rate cannot
be read as an invention rate; and that classifying it changes nothing about the
production check, which still rejects a reflowed quote.
"""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

import grounded_extraction


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "benchmark_schema_offload.py"
SPEC = importlib.util.spec_from_file_location("benchmark_schema_offload", MODULE_PATH)
bench = importlib.util.module_from_spec(SPEC)
sys.modules["benchmark_schema_offload"] = bench
SPEC.loader.exec_module(bench)


SOURCE = "Release 4.2.0 shipped on 2026-03-14 after a two week freeze."


def _case(**overrides):
    fields = {"version": {"type": "string"}, "date": {"type": "string"}}
    kwargs = {
        "name": "release",
        "source": SOURCE,
        "fields": fields,
        "expected": {"version": "4.2.0", "date": "2026-03-14"},
    }
    kwargs.update(overrides)
    return bench.Case(**kwargs)


def _reply(version_quote="Release 4.2.0 shipped", date_quote="shipped on 2026-03-14",
           version="4.2.0", date="2026-03-14"):
    return json.dumps({
        "version": {"value": version, "quote": version_quote},
        "date": {"value": date, "quote": date_quote},
    })


def _call(text=None, error=None, seen=None):
    def call_fn(**kwargs):
        if seen is not None:
            seen.append(kwargs)
        if error is not None:
            raise error
        return text
    return call_fn


def _rows(*outcomes):
    """One synthetic row per named outcome, enough for the aggregate to count."""
    return [
        {"case": "c%d" % index, "outcome": outcome, "interaction_id": "iid-%d" % index,
         "already_filed": False, "span_mix": {}, "not_run_kind": "",
         "detail": "", "parse_mode": "leading"}
        for index, outcome in enumerate(outcomes)
    ]


def _arm(name, *outcomes):
    return bench.aggregate(name, _rows(*outcomes))


# --------------------------------------------------------------------------
# A call that never ran is not a data point
# --------------------------------------------------------------------------
def test_a_call_that_never_ran_is_outside_both_sides_of_the_rate():
    arm = _arm("schema", bench.VALID, bench.VALID, bench.NOT_RUN)
    assert arm["completed"] == 2
    assert arm["not_run"] == 1
    assert arm["attempted"] == 3
    # Two of two completed calls were valid. The outage is not a failure and is
    # not a success; it is not in the fraction at all.
    assert arm["success_rate"] == 1.0


def test_the_aggregate_always_states_completed_and_not_run_next_to_the_rate():
    arm = _arm("schema", bench.VALID)
    for key in ("completed", "not_run", "attempted", "success_rate"):
        assert key in arm, key


def test_the_rate_is_unmeasured_not_zero_when_no_call_completed():
    arm = _arm("schema", bench.NOT_RUN, bench.NOT_RUN)
    # benchmark_moat returns 0.0 here, which reads as "everything failed".
    assert arm["success_rate"] is None
    assert arm["completed"] == 0
    assert arm["not_run"] == 2


def test_a_rejection_and_a_never_ran_call_are_never_the_same_bucket():
    arm = _arm("schema", bench.REJECTED, bench.NOT_RUN)
    assert arm["counts"][bench.REJECTED] == 1
    assert arm["counts"][bench.NOT_RUN] == 1
    assert arm["completed"] == 1


def test_running_and_being_rejected_is_distinct_from_running_and_being_valid():
    arm = _arm("schema", bench.VALID, bench.REJECTED)
    assert arm["completed"] == 2
    assert arm["success_rate"] == 0.5


def test_a_transport_failure_never_ran_but_a_protocol_failure_ran():
    from sonder_runtime.adapters.model_transport import ModelCallError

    timed_out = bench.run_case(
        _case(), with_schema=True,
        call_fn=_call(error=ModelCallError("timeout", "no response in 60s")),
    )
    refused = bench.run_case(
        _case(), with_schema=True,
        call_fn=_call(error=ModelCallError("protocol", "schema violation: $.date")),
    )
    assert timed_out["outcome"] == bench.NOT_RUN
    assert timed_out["not_run_kind"] == "timeout"
    assert refused["outcome"] == bench.REJECTED


# --------------------------------------------------------------------------
# The comparison refuses itself when the arms did not reach the same stage
# --------------------------------------------------------------------------
def test_a_truncated_arm_is_incomparable_rather_than_an_improvement():
    baseline = _arm(bench.ARM_NO_SCHEMA, bench.VALID, bench.WRONG, bench.WRONG, bench.WRONG)
    # One valid out of one completed looks like a perfect score. It is an outage.
    treatment = _arm(bench.ARM_SCHEMA, bench.VALID, bench.NOT_RUN, bench.NOT_RUN, bench.NOT_RUN)
    result = bench.compare_arms(baseline, treatment)
    assert result["comparable"] is False
    assert result["verdict"] == "incomparable"
    assert result["valid_delta"] is None
    assert result["success_rate_delta"] is None
    assert "4" in result["reason"] and "1" in result["reason"]


def test_arms_that_reached_the_same_stage_can_be_compared():
    baseline = _arm(bench.ARM_NO_SCHEMA, bench.VALID, bench.WRONG)
    treatment = _arm(bench.ARM_SCHEMA, bench.VALID, bench.VALID)
    result = bench.compare_arms(baseline, treatment)
    assert result["comparable"] is True
    assert result["verdict"] == "improved"
    assert result["valid_delta"] == 1


def test_a_comparison_with_nothing_completed_is_unmeasured_not_unchanged():
    baseline = _arm(bench.ARM_NO_SCHEMA, bench.NOT_RUN)
    treatment = _arm(bench.ARM_SCHEMA, bench.NOT_RUN)
    result = bench.compare_arms(baseline, treatment)
    assert result["verdict"] == "unmeasured"
    assert result["comparable"] is False


# --------------------------------------------------------------------------
# What the rendered report is allowed to say
# --------------------------------------------------------------------------
def test_the_rendered_report_states_per_arm_completion_next_to_every_rate():
    baseline = _arm(bench.ARM_NO_SCHEMA, bench.VALID, bench.WRONG)
    treatment = _arm(bench.ARM_SCHEMA, bench.VALID, bench.VALID)
    text = bench.render_report(bench.compare_arms(baseline, treatment))
    for line in text.splitlines():
        if "%" in line and "|" in line:
            assert "/2" in line or "completed" in line.lower(), line
    assert "not run" in text.lower()


def test_the_rendered_report_names_a_truncated_arm_as_truncated():
    baseline = _arm(bench.ARM_NO_SCHEMA, bench.WRONG, bench.WRONG)
    treatment = _arm(bench.ARM_SCHEMA, bench.VALID, bench.NOT_RUN)
    text = bench.render_report(bench.compare_arms(baseline, treatment))
    lowered = text.lower()
    assert "incomparable" in lowered
    assert "improve" not in lowered.split("incomparable")[0].split("verdict")[-1]


def test_the_report_names_the_caller_judged_population_and_keeps_curriculum_apart():
    baseline = _arm(bench.ARM_NO_SCHEMA, bench.VALID)
    treatment = _arm(bench.ARM_SCHEMA, bench.VALID)
    text = bench.render_report(
        bench.compare_arms(baseline, treatment),
        caller_before="caller: 10 good / 5 bad  (66.7%, n=15) - mixed",
        caller_after="caller: 11 good / 5 bad  (68.8%, n=16) - mixed",
    )
    assert "caller-judged" in text.lower()
    assert "curriculum" in text.lower()


def test_the_module_has_no_combined_figure_over_both_populations():
    assert not hasattr(bench, "overall")
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "def overall" not in source


# --------------------------------------------------------------------------
# What reaches the outcome store
# --------------------------------------------------------------------------
def test_a_never_ran_case_records_nothing_in_the_store():
    written = []
    summary = bench.record_outcomes(
        _arm("schema", bench.NOT_RUN), lambda iid, signal: written.append((iid, signal)),
    )
    assert written == []
    assert summary["skipped_not_run"] == 1


def test_a_valid_case_records_accepted_and_a_wrong_one_records_rejected():
    written = []
    bench.record_outcomes(
        _arm("schema", bench.VALID, bench.WRONG, bench.UNUSABLE),
        lambda iid, signal: written.append((iid, signal)),
    )
    assert [signal for _iid, signal in written] == ["accepted", "rejected", "rejected"]


def test_a_rejection_the_runtime_already_filed_is_not_filed_twice():
    rows = _rows(bench.REJECTED, bench.REJECTED)
    rows[0]["already_filed"] = True
    written = []
    summary = bench.record_outcomes(
        bench.aggregate("schema", rows), lambda iid, signal: written.append((iid, signal)),
    )
    assert written == [("iid-1", "rejected")]
    assert summary["skipped_already_filed"] == 1


def test_a_case_with_no_interaction_id_is_counted_rather_than_dropped():
    rows = _rows(bench.VALID)
    rows[0]["interaction_id"] = None
    written = []
    summary = bench.record_outcomes(
        bench.aggregate("schema", rows), lambda iid, signal: written.append((iid, signal)),
    )
    assert written == []
    assert summary["unrecorded_no_id"] == 1


# --------------------------------------------------------------------------
# Why a rejection happened: reflow is not invention
# --------------------------------------------------------------------------
def test_a_reflowed_quote_is_still_rejected_but_classified_as_reflow():
    case = _case()
    row = bench.run_case(
        case, with_schema=False,
        call_fn=_call(text=_reply(version_quote="Release  4.2.0\nshipped")),
    )
    assert row["outcome"] == bench.REJECTED
    assert row["span_mix"]["version"] == bench.REFLOW
    # And the production check is untouched by the classification.
    with pytest.raises(grounded_extraction.GroundingError):
        grounded_extraction.verify_grounding(
            {"version": {"value": "4.2.0", "quote": "Release  4.2.0\nshipped"}}, SOURCE,
        )


def test_an_invented_quote_is_classified_as_not_in_source():
    row = bench.run_case(
        _case(), with_schema=False,
        call_fn=_call(text=_reply(version_quote="Release 4.2.0 was cancelled")),
    )
    assert row["outcome"] == bench.REJECTED
    assert row["span_mix"]["version"] == bench.NOT_IN_SOURCE


def test_a_quote_differing_only_in_case_is_classified_as_cosmetic():
    row = bench.run_case(
        _case(), with_schema=False,
        call_fn=_call(text=_reply(version_quote="RELEASE 4.2.0 SHIPPED")),
    )
    assert row["outcome"] == bench.REJECTED
    assert row["span_mix"]["version"] == bench.COSMETIC


def test_the_rejection_mix_is_reported_per_arm():
    rows = _rows(bench.REJECTED, bench.REJECTED)
    rows[0]["span_mix"] = {"version": bench.REFLOW}
    rows[1]["span_mix"] = {"version": bench.NOT_IN_SOURCE}
    arm = bench.aggregate("schema", rows)
    assert arm["rejection_mix"][bench.REFLOW] == 1
    assert arm["rejection_mix"][bench.NOT_IN_SOURCE] == 1


# --------------------------------------------------------------------------
# Judging one reply
# --------------------------------------------------------------------------
def test_prose_around_the_json_is_parsed_rather_than_failed():
    text = "Sure! Here is the extraction:\n```json\n%s\n```\nHope that helps." % _reply()
    row = bench.run_case(_case(), with_schema=False, call_fn=_call(text=text))
    assert row["outcome"] == bench.VALID
    assert row["parse_mode"] != "leading"


def test_a_reply_with_no_json_at_all_is_unusable_not_never_ran():
    row = bench.run_case(
        _case(), with_schema=False,
        call_fn=_call(text="I could not find a version in that text."),
    )
    assert row["outcome"] == bench.UNUSABLE
    assert row["outcome"] != bench.NOT_RUN


def test_a_schema_violating_reply_is_rejected_by_the_same_check_in_both_arms():
    broken = json.dumps({"version": {"value": "4.2.0"}, "date": {"value": "x", "quote": "y"}})
    for with_schema in (False, True):
        row = bench.run_case(_case(), with_schema=with_schema, call_fn=_call(text=broken))
        assert row["outcome"] == bench.REJECTED, with_schema


def test_a_grounded_but_wrong_value_is_wrong_not_valid():
    # The span is real; the value hung off it is not what the source says. This
    # is exactly what grounding cannot catch, so the fixture catches it instead.
    row = bench.run_case(
        _case(), with_schema=True, call_fn=_call(text=_reply(version="9.9.9")),
    )
    assert row["outcome"] == bench.WRONG


def test_a_valid_reply_carries_its_interaction_id_forward():
    text = _reply() + "\n\n[interaction_id: iid-not-hex]"
    row = bench.run_case(_case(), with_schema=True, call_fn=_call(text=text))
    assert row["outcome"] == bench.VALID
    assert row["interaction_id"] == "iid-not-hex"


# --------------------------------------------------------------------------
# The fixed prompt set, and the one variable between arms
# --------------------------------------------------------------------------
def test_the_two_arms_send_the_same_prompt_and_differ_only_in_the_schema():
    seen = []
    case = _case()
    bench.run_case(case, with_schema=False, call_fn=_call(text=_reply(), seen=seen))
    bench.run_case(case, with_schema=True, call_fn=_call(text=_reply(), seen=seen))
    bare, constrained = seen
    assert bare["prompt"] == constrained["prompt"]
    assert bare["system"] == constrained["system"]
    assert bare["schema"] is None
    assert constrained["schema"] == bench.case_schema(case)


def test_the_case_set_digest_changes_with_the_prompts():
    before = bench.case_set_digest(bench.CASES)
    after = bench.case_set_digest(bench.CASES[:-1])
    assert before != after
    assert before == bench.case_set_digest(bench.CASES)


def test_every_fixture_expectation_is_literally_present_in_its_own_source():
    # An expectation the source does not state would be unachievable by any
    # model, and would show up as a permanent failure of both arms.
    assert len(bench.CASES) >= 5
    for case in bench.CASES:
        assert set(case.expected) == set(case.fields), case.name
        for name, value in case.expected.items():
            assert str(value) in case.source, (case.name, name)


# --------------------------------------------------------------------------
# Running it for real
# --------------------------------------------------------------------------
def test_a_live_run_stays_on_local_tiers():
    with pytest.raises(bench.SchemaBenchmarkError):
        bench.build_live_call("cloud-code")


def test_the_cli_without_live_reports_not_yet_run_rather_than_a_number(capsys):
    assert bench.main([]) == 0
    out = capsys.readouterr().out.lower()
    assert "not yet run" in out
    assert "success rate" not in out


def test_the_live_call_asks_the_runtime_to_learn_so_a_verdict_has_an_id(monkeypatch):
    # A verdict is filed against an interaction id, and only the learning path
    # mints one. Turning learning off would leave every row unrecordable while
    # the run still looked like it succeeded.
    import server

    seen = {}

    def fake_offload(**kwargs):
        seen.update(kwargs)
        return "{}"

    monkeypatch.setattr(server, "_offload_impl", fake_offload)
    bench.build_live_call("fast")(prompt="p", system="s", schema=None)
    assert seen["learn"] is True
    assert seen["tier"] == "fast"


def test_only_the_schema_arm_marks_a_runtime_rejection_as_already_filed():
    from sonder_runtime.adapters.model_transport import ModelCallError

    error = ModelCallError("protocol", "schema violation: $.date")
    constrained = bench.run_case(_case(), with_schema=True, call_fn=_call(error=error))
    bare = bench.run_case(_case(), with_schema=False, call_fn=_call(error=error))
    # server._file_schema_rejection fires only when a schema was supplied, so
    # re-filing on that arm would double-count; on the bare arm nobody filed it.
    assert constrained["already_filed"] is True
    assert bare["already_filed"] is False


def test_an_unknown_outcome_fails_closed_rather_than_being_ignored():
    rows = _rows(bench.VALID)
    rows[0]["outcome"] = "probably_fine"
    with pytest.raises(bench.SchemaBenchmarkError):
        bench.aggregate("schema", rows)


def test_arms_that_ran_different_cases_cannot_be_compared_at_all():
    # A truncated arm in different clothes: two rates over different work. The
    # completion counts can match exactly and the comparison still means nothing.
    baseline = bench.aggregate(bench.ARM_NO_SCHEMA, _rows(bench.VALID, bench.WRONG))
    other = _rows(bench.VALID, bench.VALID)
    other[1]["case"] = "a-case-the-other-arm-never-saw"
    treatment = bench.aggregate(bench.ARM_SCHEMA, other)
    assert baseline["completed"] == treatment["completed"]
    with pytest.raises(bench.SchemaBenchmarkError):
        bench.compare_arms(baseline, treatment)


def test_the_digest_describes_the_cases_that_actually_ran():
    def arm(name, cases):
        return bench.aggregate(name, [
            {"case": case.name, "outcome": bench.VALID, "interaction_id": "i",
             "already_filed": False, "span_mix": {}, "not_run_kind": "",
             "detail": "", "parse_mode": "leading"}
            for case in cases
        ])

    subset = bench.CASES[:3]
    partial = bench.compare_arms(
        arm(bench.ARM_NO_SCHEMA, subset), arm(bench.ARM_SCHEMA, subset),
    )
    full = bench.compare_arms(
        arm(bench.ARM_NO_SCHEMA, bench.CASES), arm(bench.ARM_SCHEMA, bench.CASES),
    )
    assert partial["case_set_digest"] == bench.case_set_digest(subset)
    assert full["case_set_digest"] == bench.case_set_digest(bench.CASES)
    assert partial["case_set_digest"] != full["case_set_digest"]
    # Cases this module does not define cannot be honestly labelled at all.
    synthetic = bench.compare_arms(_arm(bench.ARM_NO_SCHEMA, bench.VALID),
                                   _arm(bench.ARM_SCHEMA, bench.VALID))
    assert synthetic["case_set_digest"] == ""


def test_the_footer_prefix_matches_the_one_the_server_appends():
    # The judging path reads interaction ids without importing server; this is
    # the pin that keeps the copy honest.
    import server

    assert bench.FOOTER_PREFIX == server.FOOTER_PREFIX
