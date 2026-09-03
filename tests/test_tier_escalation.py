"""Bounded automatic tier escalation on the default model route.

The capability router's ladder and the gateway's escalation ceiling existed
without a live caller: a default-route turn that failed on its first model
simply returned the failure.  These tests pin the connection -- the pure plan
(rungs distinct by model, the ceiling, the reasoning pre-route, the knob),
the two chat paths stepping up on a transport failure or an empty answer and
stopping at the first answer, the served path reporting the target that
answered, and the workbench agent rerunning on the next model when its model
cannot drive the loop while an explicit tier never moves.
"""
from __future__ import annotations

import pytest

import server
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.adapters.observability import activity_tracker
from sonder_runtime.application.routing import tier_escalation as te

TIERS = {
    "fast": "m-small", "code": "m-code", "general": "m-general",
    "reasoning": "m-reasoning",
}
CODE_PROMPT = "fix the failing unit test in module.py and run pytest"
REASONING_PROMPT = "Think step by step and prove the theorem holds for n = 0."


def _resolve(tier, tiers=TIERS):
    model = tiers.get(tier)
    return te.Rung(tier=tier, model=model) if model else None


# --- the pure plan ----------------------------------------------------------


def test_plan_is_distinct_by_model_and_bounded_by_the_gateway_ceiling():
    plan = te.plan(CODE_PROMPT, start=te.Rung("sonder", "m-code"),
                   available=TIERS, resolve=_resolve)

    assert plan.task == "code"
    # The code ladder is code -> general -> reasoning; code is the start's
    # own model, so it collapses into the start rung.
    assert [rung.tier for rung in plan.rungs] == ["sonder", "general", "reasoning"]
    assert plan.escalations == te.MAX_ESCALATIONS
    assert plan.next_rung(plan.escalations) is None
    assert all(rung.augment for rung in plan.rungs)


def test_plan_is_one_rung_when_every_tier_is_the_same_model():
    same = {tier: "m-one" for tier in TIERS}
    plan = te.plan(CODE_PROMPT, start=te.Rung("sonder", "m-one"), available=same,
                   resolve=lambda tier: _resolve(tier, same))

    assert plan.rungs == (te.Rung("sonder", "m-one"),)
    assert plan.escalations == 0


def test_a_reasoning_prompt_starts_on_a_bound_reasoning_model_and_can_fall_back():
    plan = te.plan(REASONING_PROMPT, start=te.Rung("sonder", "m-code"),
                   available=TIERS, resolve=_resolve)

    assert plan.prerouted
    assert plan.start == te.Rung("reasoning", "m-reasoning")
    assert plan.rungs[1].tier == "sonder"


def test_the_reasoning_pre_route_is_skipped_when_it_is_the_start_model():
    tiers = dict(TIERS, reasoning="m-code")
    plan = te.plan(REASONING_PROMPT, start=te.Rung("sonder", "m-code"),
                   available=tiers, resolve=lambda tier: _resolve(tier, tiers))

    assert not plan.prerouted
    assert plan.start.tier == "sonder"


def test_vision_never_pre_routes_without_an_image_signal():
    tiers = dict(TIERS, vision="m-vision")
    plan = te.plan("describe the screenshot and read the text in this image",
                   start=te.Rung("sonder", "m-code"), available=tiers,
                   resolve=lambda tier: _resolve(tier, tiers))

    assert not plan.prerouted
    assert all(rung.model != "m-vision" for rung in plan.rungs)


def test_unbound_and_cloud_tiers_are_never_rungs():
    def resolve(tier):
        if tier == "general":
            return None
        if tier == "reasoning":
            return te.Rung("reasoning", "big-cloud", cloud=True)
        return _resolve(tier)

    plan = te.plan(CODE_PROMPT, start=te.Rung("sonder", "m-code"),
                   available=TIERS, resolve=resolve)

    assert plan.rungs == (te.Rung("sonder", "m-code"),)


def test_a_cloud_start_never_escalates():
    plan = te.plan(CODE_PROMPT, start=te.Rung("cloud-code", "big-cloud", cloud=True),
                   available=TIERS, resolve=_resolve)

    assert plan.rungs == (te.Rung("cloud-code", "big-cloud", cloud=True),)


def test_the_budget_is_clamped_to_the_gateway_ceiling():
    wide = te.plan(CODE_PROMPT, start=te.Rung("sonder", "m-code"), available=TIERS,
                   resolve=_resolve, max_escalations=10)
    none = te.plan(CODE_PROMPT, start=te.Rung("sonder", "m-code"), available=TIERS,
                   resolve=_resolve, max_escalations=0)

    assert wide.escalations <= te.MAX_ESCALATIONS
    assert none.escalations == 0


@pytest.mark.parametrize("error, response, expected", [
    (ModelCallError("protocol", "bad json"), None, "failed"),
    (ModelCallError("timeout", "slow"), None, "failed"),
    (ModelCallError("empty_response", "nothing"), None, "empty_response"),
    (ModelCallError("cancelled", "stop"), None, None),
    (ModelCallError("budget", "spent"), None, None),
    (ModelCallError("protocol", "remote", cloud=True), None, None),
    (None, "", "empty_response"),
    (None, "   \n", "empty_response"),
    (None, "an answer", None),
    (None, None, None),
])
def test_failure_reasons(error, response, expected):
    assert te.failure_reason(error=error, response=response) == expected


@pytest.mark.parametrize("value, expected", [
    (None, True), ("", True), ("1", True), ("on", True),
    ("0", False), ("off", False), ("FALSE", False), (" no ", False),
])
def test_the_knob_defaults_on_and_reads_the_usual_off_tokens(value, expected):
    assert te.enabled(value) is expected


def test_the_escalation_line_names_both_rungs_and_the_reason():
    step = te.Step(1, "failed", te.Rung("sonder", "m-code"), te.Rung("general", "m-general"))

    assert te.describe([step]) == "model escalation: sonder (m-code) -> general (m-general): failed"
    assert te.describe([]) == ""


# --- the chat paths -----------------------------------------------------------


class _Connection:
    def close(self):
        return None


def _install_chat_fakes(monkeypatch, answers, *, target=("m-code", False, True, "sonder")):
    """Fake the default route: ``answers`` maps model -> text or exception."""
    calls = []
    discarded = []
    monkeypatch.delenv(te.KNOB, raising=False)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "control_command", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_route_chat_web", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_web_denial_guard", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(server, "_serve_target", lambda requested, strict: target)
    monkeypatch.setattr(server, "TIERS", dict(TIERS))
    monkeypatch.setattr(server, "_configured_local_tiers", lambda: tuple(TIERS))
    monkeypatch.setattr(server, "_build_system", lambda *args, **kwargs: "")
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 4096)
    monkeypatch.setattr(server, "_internal_generate_for_route", lambda model, cloud: None)
    monkeypatch.setattr(server, "_open_db", _Connection)
    monkeypatch.setattr(server, "_session_history_messages", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_maybe_title", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_should_learn", lambda *args: True)
    monkeypatch.setattr(server, "_apply_code_gate",
                        lambda response, **kwargs: (response, False, False))
    monkeypatch.setattr(server, "_capture_durable_session_turn", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_discard_interaction", discarded.append)
    monkeypatch.setattr(server.memory_store, "session_turn_count", lambda *args: 0)
    monkeypatch.setattr(server.memory_store, "touch_session", lambda *args: None)
    monkeypatch.setattr(server.memory_store, "get_interaction", lambda *args: {})

    def answer(conn, prompt, model, effective_system, temperature, num_predict,
               num_ctx, session_id, project, history, trace=False, tier="sonder",
               cloud=False, augment=True, allow_cloud_fallback=True):
        calls.append({"model": model, "tier": tier, "augment": augment})
        outcome = answers[model]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome, "iid-%s" % model, None

    monkeypatch.setattr(server, "_answer", answer)
    return calls, discarded


def _escalation_events(response):
    return [event for event in response["events"] if event["kind"] == "model_escalation"]


def test_mcp_chat_steps_up_when_the_first_model_fails(monkeypatch):
    calls, _discarded = _install_chat_fakes(monkeypatch, {
        "m-code": ModelCallError("protocol", "no usable reply"),
        "m-general": "answer from general",
    })

    with activity_tracker.response_span("chat", CODE_PROMPT, surface="test") as response:
        out = server._sonder_impl_serialized(CODE_PROMPT, session="none")

    assert "answer from general" in out
    assert [call["model"] for call in calls] == ["m-code", "m-general"]
    # The escalated rung keeps the default route's augmentation and is
    # recorded under its own tier label.
    assert calls[1] == {"model": "m-general", "tier": "general", "augment": True}
    events = _escalation_events(response)
    assert len(events) == 1
    assert events[0]["summary"] == "chat: sonder (m-code) -> general (m-general): failed"
    assert events[0]["model"] == "m-general"


def test_mcp_chat_discards_an_empty_answer_before_stepping_up(monkeypatch):
    calls, discarded = _install_chat_fakes(monkeypatch, {
        "m-code": "   ",
        "m-general": "answer from general",
    })

    out = server._sonder_impl_serialized(CODE_PROMPT, session="none")

    assert "answer from general" in out
    assert [call["model"] for call in calls] == ["m-code", "m-general"]
    assert discarded == ["iid-m-code"]


def test_mcp_chat_stops_at_the_first_answer(monkeypatch):
    calls, _discarded = _install_chat_fakes(monkeypatch, {"m-code": "fine"})

    out = server._sonder_impl_serialized(CODE_PROMPT, session="none")

    assert "fine" in out
    assert [call["model"] for call in calls] == ["m-code"]


def test_mcp_chat_returns_the_last_failure_when_the_ladder_is_spent(monkeypatch):
    calls, _discarded = _install_chat_fakes(monkeypatch, {
        "m-code": ModelCallError("protocol", "first"),
        "m-general": ModelCallError("protocol", "second"),
        "m-reasoning": ModelCallError("protocol", "third"),
    })

    out = server._sonder_impl_serialized(CODE_PROMPT, session="none")

    assert out.startswith("ERROR")
    assert "third" in out
    assert len(calls) == 1 + te.MAX_ESCALATIONS


def test_a_reasoning_prompt_is_answered_by_the_reasoning_model_first(monkeypatch):
    calls, _discarded = _install_chat_fakes(monkeypatch, {"m-reasoning": "proved"})

    out = server._sonder_impl_serialized(REASONING_PROMPT, session="none")

    assert "proved" in out
    assert [call["model"] for call in calls] == ["m-reasoning"]
    assert calls[0]["tier"] == "reasoning" and calls[0]["augment"] is True


def test_an_explicit_tier_never_escalates(monkeypatch):
    calls, _discarded = _install_chat_fakes(
        monkeypatch, {"m-code": ModelCallError("protocol", "first")},
        target=("m-code", False, False, "code"),
    )

    out = server._sonder_impl_serialized(CODE_PROMPT, session="none", tier="code")

    assert out.startswith("ERROR")
    assert [call["model"] for call in calls] == ["m-code"]


def test_the_knob_turns_escalation_off(monkeypatch):
    calls, _discarded = _install_chat_fakes(monkeypatch, {
        "m-code": ModelCallError("protocol", "first"),
        "m-general": "unreached",
    })
    monkeypatch.setenv(te.KNOB, "0")

    out = server._sonder_impl_serialized(CODE_PROMPT, session="none")

    assert out.startswith("ERROR")
    assert [call["model"] for call in calls] == ["m-code"]


def test_a_cancellation_is_never_retried_on_another_model(monkeypatch):
    calls, _discarded = _install_chat_fakes(monkeypatch, {
        "m-code": ModelCallError("cancelled", "stop"),
        "m-general": "unreached",
    })

    server._sonder_impl_serialized(CODE_PROMPT, session="none")

    assert [call["model"] for call in calls] == ["m-code"]


def test_served_chat_steps_up_and_reports_the_target_that_answered(monkeypatch):
    calls, _discarded = _install_chat_fakes(monkeypatch, {
        "m-code": ModelCallError("protocol", "no usable reply"),
        "m-general": "answer from general",
    })
    observed = []

    out = server._answer_with_history_impl(
        CODE_PROMPT, [], tier="", raise_model_errors=True,
        target_observer=lambda model, label, cloud: observed.append((model, label, cloud)),
    )

    assert "answer from general" in out
    assert [call["model"] for call in calls] == ["m-code", "m-general"]
    assert observed == [("m-code", "sonder", False), ("m-general", "general", False)]


def test_served_chat_reports_a_pre_routed_reasoning_target(monkeypatch):
    """Measured 2026-09-03: the receipt named the default route while the
    reasoning model answered."""
    calls, _discarded = _install_chat_fakes(monkeypatch, {"m-reasoning": "proved"})
    observed = []

    out = server._answer_with_history_impl(
        REASONING_PROMPT, [], tier="", raise_model_errors=True,
        target_observer=lambda model, label, cloud: observed.append((model, label, cloud)),
    )

    assert "proved" in out
    assert [call["model"] for call in calls] == ["m-reasoning"]
    assert observed == [("m-reasoning", "reasoning", False)]


def test_served_chat_with_an_explicit_model_field_raises_its_own_failure(monkeypatch):
    calls, _discarded = _install_chat_fakes(
        monkeypatch, {"m-code": ModelCallError("protocol", "first")},
        target=("m-code", False, False, "code"),
    )

    with pytest.raises(ModelCallError, match="first"):
        server._answer_with_history_impl(CODE_PROMPT, [], tier="code", raise_model_errors=True)

    assert [call["model"] for call in calls] == ["m-code"]


# --- the workbench agent ------------------------------------------------------


def _install_agent_fakes(monkeypatch, replies):
    """``replies`` maps model -> the text every model call returns."""
    factory_models = []
    monkeypatch.delenv(te.KNOB, raising=False)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "TIERS", dict(TIERS))
    monkeypatch.setattr(server, "_configured_local_tiers", lambda: tuple(TIERS))
    monkeypatch.setattr(server, "_runtime_lane_tier", lambda lane, requested="": (
        requested if requested and requested not in ("auto", "default", "policy") else "code"
    ))

    def make_generate(model, *args, **kwargs):
        factory_models.append(model)
        return lambda prompt, history=None: replies[model]

    monkeypatch.setattr(server, "_make_generate", make_generate)
    return factory_models


def test_workbench_agent_reruns_on_the_next_model_when_its_model_cannot_drive_the_loop(
    monkeypatch, tmp_path,
):
    factory_models = _install_agent_fakes(monkeypatch, {
        "m-code": "I would rather describe the plan than emit JSON.",
        "m-general": '{"final":"general finished the work"}',
    })

    out = server.workbench_agent(
        prompt="inspect the repository and list its files", tier="auto",
        project=str(tmp_path),
    )

    assert out.startswith("model escalation: code (m-code) -> general (m-general): failed")
    assert "general finished the work" in out
    assert factory_models[0] == "m-code"
    assert "m-general" in factory_models
    assert server._take_agent_model_failure() is None


def test_workbench_agent_keeps_a_finished_run(monkeypatch, tmp_path):
    factory_models = _install_agent_fakes(monkeypatch, {
        "m-code": '{"final":"nothing to do here"}',
    })

    out = server.workbench_agent(
        prompt="inspect the repository and list its files", tier="auto",
        project=str(tmp_path),
    )

    assert "nothing to do here" in out
    assert "model escalation" not in out
    assert set(factory_models) == {"m-code"}


def test_workbench_agent_with_an_explicit_tier_never_escalates(monkeypatch, tmp_path):
    factory_models = _install_agent_fakes(monkeypatch, {
        "m-code": "still not JSON",
        "m-general": '{"final":"unreached"}',
    })

    out = server.workbench_agent(
        prompt="inspect the repository and list its files", tier="code",
        project=str(tmp_path),
    )

    assert "could not parse agent decision" in out
    assert "unreached" not in out
    assert set(factory_models) == {"m-code"}


def test_workbench_agent_reports_the_last_failure_when_every_rung_fails(
    monkeypatch, tmp_path,
):
    factory_models = _install_agent_fakes(monkeypatch, {
        "m-code": "no", "m-general": "no", "m-reasoning": "no",
    })

    out = server.workbench_agent(
        prompt="inspect the repository and list its files", tier="auto",
        project=str(tmp_path),
    )

    assert out.startswith("ERROR: could not parse agent decision")
    assert out.rstrip().endswith(
        "model escalation: code (m-code) -> general (m-general): failed; "
        "general (m-general) -> reasoning (m-reasoning): failed"
    )
    assert factory_models.count("m-code") >= 1
    assert "m-reasoning" in factory_models


@pytest.mark.parametrize("prompt, expected", [
    ("fix the bug in app.py and run the tests", True),
    ("add a trial_balance function to ledger/core.py", True),
    ("run the tests in tests/", True),
    ("read app.py and explain what it does", False),
    ("inspect the repository and list its files", False),
    ("search the repo for TODO markers", False),
])
def test_work_expects_effects_reads_the_request_verbs(prompt, expected):
    assert server._work_expects_effects(prompt) is expected


def test_a_completion_claim_without_a_change_or_a_validation_escalates_a_change_request(
    monkeypatch, tmp_path,
):
    """Measured 2026-09-03: a 1.5B 'completed' a fix after changing nothing."""
    factory_models = _install_agent_fakes(monkeypatch, {
        "m-code": '{"final":"done"}',
        "m-general": '{"final":"done"}',
        "m-reasoning": '{"final":"done"}',
    })

    out = server.workbench_agent(
        prompt="fix the bug in app.py and run the tests", tier="auto",
        project=str(tmp_path),
    )

    assert out.rstrip().endswith(
        "model escalation: code (m-code) -> general (m-general): failed "
        "(claimed completion without a change or a validation); "
        "general (m-general) -> reasoning (m-reasoning): failed "
        "(claimed completion without a change or a validation)"
    )
    assert {"m-code", "m-general", "m-reasoning"} <= set(factory_models)


def test_a_completion_claim_on_a_read_request_stands(monkeypatch, tmp_path):
    factory_models = _install_agent_fakes(monkeypatch, {
        "m-code": '{"final":"the file defines two functions"}',
        "m-general": '{"final":"unreached"}',
    })

    out = server.workbench_agent(
        prompt="read app.py and explain what it does", tier="auto",
        project=str(tmp_path),
    )

    assert "the file defines two functions" in out
    assert "model escalation" not in out
    assert set(factory_models) == {"m-code"}


def test_routed_work_names_the_tier_that_answered(monkeypatch):
    seen = []
    monkeypatch.delenv(te.KNOB, raising=False)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "TIERS", dict(TIERS))
    monkeypatch.setattr(server, "_configured_local_tiers", lambda: tuple(TIERS))
    monkeypatch.setattr(
        server, "_execution_route_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("not needed")),
    )

    def fake_workbench(**kwargs):
        seen.append(kwargs["tier"])
        if kwargs["tier"] == "code":
            server._note_agent_model_failure(
                te.REASON_FAILED,
                key=server._agent_escalation_key("code", kwargs["prompt"]), step=1,
            )
            return "ERROR: could not parse agent decision at step 1"
        return "work complete"

    monkeypatch.setattr(server, "workbench_agent", fake_workbench)

    output = server.route_work_request("Build the Flutter app.", project="demo")

    assert seen == ["code", "general"]
    assert "tier: general -> m-general" in output
    assert "model escalation: code (m-code) -> general (m-general): failed" in output
    assert output.rstrip().endswith("work complete")
