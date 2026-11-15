"""Pure batch-job rules: strict templates, dependencies, ordering, progress and tokens."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.batch_jobs import (
    ItemSpec, Limits, StepState, StepStatus, counts, effective_statuses,
    is_finished, next_step, progress, render_prompt, served_model_counts, token_rollup,
    validate_items, validate_spec,
)
from sonder_runtime.domain.common.errors import InvalidInput

LIMITS = Limits()


def _cloud(name):
    return name.endswith(":cloud")


def _spec(stages, fields=("text",), name="job"):
    return {"name": name, "fields": list(fields), "stages": stages}


def _stage(name, template="{{text}}", model="m:1", **extra):
    return {"name": name, "model": model, "prompt": template, **extra}


def _validate(stages, **kwargs):
    return validate_spec(_spec(stages, **kwargs), LIMITS, is_cloud_model=_cloud)


def test_valid_two_stage_spec_derives_dependencies_from_references():
    spec = _validate([_stage("r1"), _stage("r2", "{{text}} / {{stage.r1}}", model="m:2")])
    assert [stage.name for stage in spec.stages] == ["r1", "r2"]
    assert spec.stages[1].depends_on == ("r1",)
    assert spec.stages[0].depends_on == ()


@pytest.mark.parametrize("stages", [
    [_stage("r1", "{{nope}}")],                                   # unknown field
    [_stage("r1", "{{stage.r2}}"), _stage("r2")],                 # forward reference
    [_stage("r1", "{{stage.r1}}")],                               # self reference
    [_stage("r1", "{{stage.ghost}}")],                            # unknown stage
    [_stage("r1", "{{ bad token }}")],                            # malformed placeholder
    [_stage("r1"), _stage("r2"), _stage("r3", "{{stage.r1}}", depends_on=["r2"])],  # non-ancestor
    [_stage("r1"), _stage("r1")],                                 # duplicate stage name
    [_stage("r1", model="m:cloud")],                              # cloud model
    [_stage("r1", depends_on=["r2"]), _stage("r2")],              # forward depends_on
    [],                                                           # no stages
])
def test_invalid_specs_are_refused_with_invalid_input(stages):
    with pytest.raises(InvalidInput) as caught:
        _validate(stages)
    assert caught.value.code == "INVALID_INPUT"


def test_cloud_model_refusal_names_the_stage_and_never_reaches_the_runner():
    with pytest.raises(InvalidInput, match="cloud model"):
        _validate([_stage("r1"), _stage("r2", model="kimi:cloud")])


def test_transitive_ancestor_reference_is_allowed():
    spec = _validate([
        _stage("r1"), _stage("r2", "{{stage.r1}}"), _stage("r3", "{{stage.r1}} {{stage.r2}}", depends_on=["r2"]),
    ])
    assert spec.stages[2].depends_on == ("r2",)


def test_stage_limits_and_types_are_enforced():
    with pytest.raises(InvalidInput):
        validate_spec(_spec([_stage("s%d" % i) for i in range(9)]), LIMITS, is_cloud_model=_cloud)
    for bad in ({"num_predict": True}, {"num_predict": 31}, {"temperature": 3}, {"think": "yes"},
                {"timeout_s": 901}, {"num_ctx": "4096"}):
        with pytest.raises(InvalidInput):
            _validate([_stage("r1", **bad)])


def test_render_is_one_pass_and_never_expands_substituted_values():
    spec = _validate([_stage("r1"), _stage("r2", "A={{text}} B={{stage.r1}}")])
    item = ItemSpec("i1", {"text": "see {{stage.r1}} and {{text}}"})
    system, prompt = render_prompt(spec.stages[1], item, {"r1": "up {{text}} {{stage.r1}}"})
    assert system == ""
    assert prompt == "A=see {{stage.r1}} and {{text}} B=up {{text}} {{stage.r1}}"


def test_system_prompt_uses_the_same_strict_placeholders():
    spec = _validate([_stage("r1", system="Role for {{text}}")])
    assert render_prompt(spec.stages[0], ItemSpec("i", {"text": "x"}), {})[0] == "Role for x"
    with pytest.raises(InvalidInput):
        _validate([_stage("r1", system="Role {{missing}}")])


def test_items_must_match_declared_fields_and_have_unique_keys():
    spec = _validate([_stage("r1")])
    good = [{"key": "a", "fields": {"text": "x"}}, {"key": "b", "fields": {"text": "y"}}]
    assert [item.key for item in validate_items(good, spec, LIMITS)] == ["a", "b"]
    for bad in (
        [{"key": "a", "fields": {"text": "x"}}, {"key": "a", "fields": {"text": "y"}}],
        [{"key": "a", "fields": {"other": "x"}}],
        [{"key": "a", "fields": {"text": 5}}],
        [{"key": "a", "fields": {"text": "x"}, "extra": 1}],
        [{"key": "", "fields": {"text": "x"}}],
        [{"key": "a", "fields": {"text": "x" * (LIMITS.max_item_input_chars + 1)}}],
    ):
        with pytest.raises(InvalidInput):
            validate_items(bad, spec, LIMITS)
    with pytest.raises(InvalidInput):
        validate_items(good, spec, LIMITS, existing_keys=["b"])


def _two_stage():
    return _validate([_stage("r1"), _stage("r2", "{{stage.r1}}", model="m:2")])


def _state(stage, key, status, finished=None, **extra):
    return StepState(stage=stage, item_key=key, status=status, finished_ts=finished, **extra)


def test_stage_major_runs_every_stage_one_step_before_stage_two():
    spec, keys = _two_stage(), ["a", "b", "c"]
    states, order = {}, []
    while True:
        effective = effective_statuses(spec, keys, states)
        step = next_step(spec, keys, effective)
        if step is None:
            break
        order.append(step)
        states[step] = _state(step[0], step[1], StepStatus.ANSWERED)
    assert order == [("r1", "a"), ("r1", "b"), ("r1", "c"), ("r2", "a"), ("r2", "b"), ("r2", "c")]


def test_item_major_order_is_available():
    spec, keys = _two_stage(), ["a", "b"]
    states, order = {}, []
    while (step := next_step(spec, keys, effective_statuses(spec, keys, states), order="item_major")):
        order.append(step)
        states[step] = _state(step[0], step[1], StepStatus.ANSWERED)
    assert order == [("r1", "a"), ("r2", "a"), ("r1", "b"), ("r2", "b")]


@pytest.mark.parametrize("upstream", [StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.UNKNOWN])
def test_a_failed_skipped_or_unknown_upstream_blocks_the_downstream_step(upstream):
    spec, keys = _two_stage(), ["a", "b"]
    states = {("r1", "a"): _state("r1", "a", upstream), ("r1", "b"): _state("r1", "b", StepStatus.ANSWERED)}
    effective = effective_statuses(spec, keys, states)
    assert effective[("r2", "a")] is StepStatus.BLOCKED
    assert next_step(spec, keys, effective) == ("r2", "b")
    states[("r2", "b")] = _state("r2", "b", StepStatus.ANSWERED)
    effective = effective_statuses(spec, keys, states)
    assert next_step(spec, keys, effective) is None and is_finished(effective)
    assert counts(effective)["blocked"] == 1


def test_blocked_follows_through_a_chain_and_pending_upstream_is_not_blocked():
    spec = _validate([_stage("r1"), _stage("r2", "{{stage.r1}}"), _stage("r3", "{{stage.r2}}")])
    effective = effective_statuses(spec, ["a"], {("r1", "a"): _state("r1", "a", StepStatus.FAILED)})
    assert effective[("r2", "a")] is StepStatus.BLOCKED and effective[("r3", "a")] is StepStatus.BLOCKED
    pending = effective_statuses(spec, ["a"], {})
    assert pending[("r2", "a")] is StepStatus.PENDING


def test_progress_has_no_eta_below_the_sample_floor_and_blocked_counts_as_done():
    spec, keys = _two_stage(), ["a", "b", "c"]
    states = {
        ("r1", "a"): _state("r1", "a", StepStatus.ANSWERED, finished=100.0),
        ("r1", "b"): _state("r1", "b", StepStatus.FAILED, finished=110.0),
        ("r1", "c"): _state("r1", "c", StepStatus.ANSWERED, finished=120.0),
    }
    report = progress(spec, keys, states, segment_start=0.0, eta_window=20, eta_min_samples=4)
    assert report["rate_per_min"] is None and report["eta_s"] is None
    assert report["eta_basis"] == "insufficient_samples" and report["samples"] == 3
    assert report["total"] == 6 and report["blocked"] == 1 and report["done"] == 4
    ready = progress(spec, keys, states, segment_start=0.0, eta_window=20, eta_min_samples=3)
    assert ready["rate_per_min"] == pytest.approx(6.0) and ready["eta_basis"] == "recent_window"
    assert ready["eta_s"] == pytest.approx(2 / 0.1)


def test_rate_counts_only_completions_in_the_current_segment():
    spec, keys = _validate([_stage("r1")]), ["a", "b", "c", "d"]
    states = {
        ("r1", "a"): _state("r1", "a", StepStatus.ANSWERED, finished=1.0),
        ("r1", "b"): _state("r1", "b", StepStatus.ANSWERED, finished=2.0),
        ("r1", "c"): _state("r1", "c", StepStatus.ANSWERED, finished=1000.0),
        ("r1", "d"): _state("r1", "d", StepStatus.ANSWERED, finished=1010.0),
    }
    report = progress(spec, keys, states, segment_start=900.0, eta_window=20, eta_min_samples=3)
    assert report["samples"] == 2 and report["rate_per_min"] is None
    assert report["done"] == 4 and report["eta_s"] == 0.0 and report["eta_basis"] == "complete"


def test_token_rollup_keeps_provider_and_estimated_apart_and_unknown_is_not_zero():
    states = [
        _state("r1", "a", StepStatus.ANSWERED, tokens_in=10, tokens_out=5, token_source="ollama"),
        _state("r1", "b", StepStatus.ANSWERED, tokens_in=7, tokens_out=3, token_source="estimated"),
        _state("r1", "c", StepStatus.ANSWERED, tokens_in=4, tokens_out=2, token_source="mixed"),
        _state("r1", "d", StepStatus.ANSWERED),
        _state("r1", "e", StepStatus.FAILED),
    ]
    assert token_rollup(states) == {
        "tokens_in_provider": 10, "tokens_out_provider": 5, "tokens_in_estimated": 11,
        "tokens_out_estimated": 5, "rows_with_estimated_tokens": 2, "rows_with_unknown_tokens": 1,
    }


def test_served_model_counts_show_a_substitution():
    states = [
        _state("r1", "a", StepStatus.ANSWERED, served_model="m:1"),
        _state("r1", "b", StepStatus.ANSWERED, served_model="m:9"),
        _state("r1", "c", StepStatus.ANSWERED, served_model="m:1"),
        _state("r1", "d", StepStatus.FAILED),
    ]
    assert served_model_counts(states) == {"m:1": 2, "m:9": 1}
