"""Opt-in long-context overflow: decision, notice, policy, availability, telemetry."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import sonder_runtime.adapters.runtime_policy as runtime_policy
from sonder_runtime.adapters.inference.overflow_availability import pool_model_availability
from sonder_runtime.application.observability import runtime_telemetry as rt
from sonder_runtime.application.routing import long_context_overflow as overflow
from sonder_runtime.application.routing import tier_escalation
from sonder_runtime.application.session import provider_attempts as attempts
from sonder_runtime.domain.context_formatting import rough_token_count
from sonder_runtime.domain.runtime_policy import rules

MOE = "qwen3.6:35b"
DENSE = "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q3_K_XL"
ON = {"enabled": True, "threshold_tokens": 32768, "model": MOE, "provider": "ollama"}
START = tier_escalation.Rung(tier="general", model=DENSE)


def _available(model):
    return overflow.Availability(overflow.AVAILABLE, worker="10.77.0.2:8443")


def _decide(settings=ON, rung=START, tokens=41_234, provider="sonder_inference", **extra):
    return overflow.decide(
        settings, rung=rung, provider=provider, estimated_tokens=tokens,
        availability=extra.pop("availability", _available), **extra,
    )


# -- decision ------------------------------------------------------------------

def test_under_threshold_keeps_the_route():
    assert _decide(tokens=32768) is None
    assert _decide(tokens=1_000) is None


def test_disabled_never_switches():
    assert _decide(settings={**ON, "enabled": False}) is None


def test_over_threshold_switches_a_local_tier_to_the_pool_model():
    decision = _decide()
    assert decision.switched
    assert (decision.from_model, decision.from_provider) == (DENSE, "sonder_inference")
    assert (decision.to_model, decision.to_provider) == (MOE, "ollama")
    assert decision.worker == "10.77.0.2:8443"
    assert decision.notice() == (
        "long-context overflow: switched to qwen3.6:35b on 10.77.0.2:8443 "
        "— context 41.2k tokens > 32.8k threshold"
    )


def test_explicit_model_pin_is_untouched():
    assert _decide(explicit_pin=True) is None
    pinned = tier_escalation.Rung(tier="model:pinned:7b", model="pinned:7b")
    assert _decide(rung=pinned) is None


def test_cloud_target_is_untouched():
    cloud = tier_escalation.Rung(tier="cloud-code", model="big:cloud", cloud=True)
    assert _decide(rung=cloud, provider=None) is None
    code = tier_escalation.Rung(tier="code", model="x:cloud", cloud=True)
    assert _decide(rung=code, provider=None) is None


def test_vision_tier_is_untouched():
    assert _decide(rung=tier_escalation.Rung(tier="vision", model="vl:7b")) is None


def test_already_on_the_overflow_model_is_a_no_op():
    reasoning = tier_escalation.Rung(tier="reasoning", model=MOE)
    assert _decide(rung=reasoning, provider=None) is None


def test_unavailable_model_stays_and_says_why():
    def none(model):
        return overflow.Availability(
            overflow.UNAVAILABLE, reason="no eligible Ollama pool worker advertises " + model,
        )

    decision = _decide(availability=none)
    assert decision.status == "unavailable" and not decision.switched
    plan = tier_escalation.single(START)
    assert overflow.apply(plan, decision) is plan
    assert decision.notice() == (
        "long-context overflow (qwen3.6:35b) unavailable, stayed on %s: no eligible "
        "Ollama pool worker advertises qwen3.6:35b — context 41.2k tokens > "
        "32.8k threshold" % DENSE
    )
    assert decision.receipt()["reason"].startswith("no eligible Ollama pool worker")


def test_failing_availability_probe_is_unavailable_not_an_error():
    def boom(model):
        raise RuntimeError("pool gone")

    decision = _decide(availability=boom)
    assert decision.status == "unavailable"
    assert decision.reason == "worker availability check failed"


def test_empty_model_uses_the_reasoning_tier_or_says_none_is_configured():
    settings = {**ON, "model": ""}
    assert _decide(settings=settings, reasoning_model=MOE).to_model == MOE
    missing = _decide(settings=settings)
    assert missing.status == "unavailable"
    assert "no overflow model configured" in missing.reason


def test_apply_puts_the_overflow_rung_first_and_keeps_the_original_route():
    reasoning = tier_escalation.Rung(tier="reasoning", model=MOE)
    plan = tier_escalation.Plan(task="long_context", confidence=0.9, rungs=(START, reasoning))
    applied = overflow.apply(plan, _decide())
    assert [rung.model for rung in applied.rungs] == [MOE, DENSE]
    assert overflow.is_overflow(applied.rungs[0])
    assert applied.rungs[0].tier == "general" and not applied.rungs[0].cloud
    assert not overflow.is_overflow(applied.rungs[1])


def test_failed_overflow_attempt_settles_as_stayed():
    decision = _decide()
    applied = overflow.apply(tier_escalation.single(START), decision)
    step = tier_escalation.Step(1, "failed", applied.rungs[0], applied.rungs[1],
                                detail="connection: Ollama worker pool unavailable")
    failure = overflow.failure_text(step)
    settled = overflow.settle(decision, applied.rungs[1], failure)
    assert settled.status == "unavailable" and settled.from_model == DENSE
    assert "stayed on %s: overflow attempt failed (connection" % DENSE in settled.notice()
    assert overflow.settle(decision, applied.rungs[0]) is decision
    assert overflow.failure_text(tier_escalation.Step(1, "failed", START, START)) == ""


def test_error_detail_only_describes_the_overflow_rung():
    error = SimpleNamespace(kind="connection", detail="pool rejected")
    overflow_rung = tier_escalation.Rung(tier="general", model=MOE, route=overflow.ROUTE)
    assert overflow.error_detail(overflow_rung, error) == "connection: pool rejected"
    assert overflow.error_detail(START, error) == ""


def test_plan_turn_estimates_history_and_prompt():
    history = [{"role": "user", "content": "x" * 140_000},
               {"role": "assistant", "content": [{"type": "text", "text": "y" * 400}]}]
    plan, decision = overflow.plan_turn(
        ON, tier_escalation.single(START), prompt="hello", history=history,
        provider_for=lambda rung: "sonder_inference", availability=_available,
    )
    expected = rough_token_count("x" * 140_000) + rough_token_count("y" * 400) + rough_token_count("hello")
    assert decision.estimated_tokens == expected
    assert overflow.is_overflow(plan.start)
    same, none = overflow.plan_turn(
        {**ON, "enabled": False}, tier_escalation.single(START), prompt="hi",
        history=history, provider_for=lambda rung: None, availability=_available,
    )
    assert none is None and same.start is START


def test_context_window_fits_the_prompt_and_respects_the_ceiling():
    assert overflow.context_window(41_234, 16_384, ceiling=262_144) == 53_248
    assert overflow.context_window(1_000, 65_536, ceiling=262_144) == 65_536
    assert overflow.context_window(900_000, 16_384, ceiling=262_144) == 262_144


def test_receipt_scope_carries_the_final_decision_only():
    decision = _decide()
    assert overflow.record(decision) is False
    with overflow.notice_scope() as notes:
        assert overflow.record(decision) is True
        overflow.record(decision.stayed("empty_response"))
        entry = overflow.receipt_entry(notes)
    assert entry["status"] == "unavailable"
    assert entry["estimated_tokens"] == 41_234 and entry["threshold_tokens"] == 32_768
    assert entry["notice"].startswith("long-context overflow (qwen3.6:35b) unavailable")
    assert overflow.receipt_entry([]) is None


# -- policy ---------------------------------------------------------------------

@pytest.fixture
def policy_file(monkeypatch, tmp_path):
    path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(path))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    for name in rules.OVERFLOW_ENV.values():
        monkeypatch.delenv(name, raising=False)
    return path


def test_policy_defaults_to_disabled(policy_file):
    policy = runtime_policy.load(create=True)
    assert policy["long_context_overflow"] == {
        "enabled": False, "threshold_tokens": 32768, "model": "", "provider": "ollama",
    }
    assert "long-context overflow: off" in runtime_policy.format_policy(policy)


def test_policy_update_persists_and_validates(policy_file):
    updated = runtime_policy.update(
        long_context_overflow={"enabled": True, "model": MOE, "threshold_tokens": 40000},
    )
    assert updated["long_context_overflow"]["enabled"] is True
    assert runtime_policy.load(create=False)["long_context_overflow"]["model"] == MOE
    for bad in ({"threshold_tokens": 4095}, {"model": "qwen3-coder:480b-cloud"},
                {"provider": "openai"}, {"surprise": 1}, {"enabled": "maybe"}):
        with pytest.raises(ValueError):
            runtime_policy.update(long_context_overflow=bad)


def test_environment_overrides_the_policy(policy_file, monkeypatch):
    policy = runtime_policy.load(create=True)
    monkeypatch.setenv("SONDER_LONG_CONTEXT_OVERFLOW", "1")
    monkeypatch.setenv("SONDER_LONG_CONTEXT_THRESHOLD", "20000")
    monkeypatch.setenv("SONDER_LONG_CONTEXT_MODEL", MOE)
    settings = runtime_policy.long_context_overflow(policy)
    assert settings["enabled"] is True and settings["threshold_tokens"] == 20000
    assert settings["model"] == MOE
    assert settings["overrides"] == ("enabled", "threshold_tokens", "model")
    assert "environment overrides" in runtime_policy.format_long_context_overflow(policy)


def test_invalid_environment_override_never_enables_or_names_cloud(policy_file, monkeypatch):
    policy = runtime_policy.load(create=True)
    monkeypatch.setenv("SONDER_LONG_CONTEXT_OVERFLOW", "sure")
    monkeypatch.setenv("SONDER_LONG_CONTEXT_THRESHOLD", "100")
    monkeypatch.setenv("SONDER_LONG_CONTEXT_MODEL", "big:cloud")
    settings = runtime_policy.long_context_overflow(policy)
    assert settings["enabled"] is False
    assert settings["threshold_tokens"] == 32768 and settings["model"] == ""
    assert settings["overrides"] == ()
    assert "cloud" in settings["error"]


# -- availability -----------------------------------------------------------------

def _snapshot(worker, state="ready", models=(), healthy=True):
    return SimpleNamespace(worker_id=worker, state=state, models=tuple(models), healthy=healthy)


class _Pool:
    def __init__(self, *snapshots, enabled=True):
        self._snapshots = snapshots
        self.enabled = enabled

    def snapshots(self):
        return self._snapshots


def test_pool_worker_advertising_the_model_is_named():
    pool = _Pool(_snapshot("127.0.0.1:11434", models=(DENSE,)),
                 _snapshot("10.77.0.2:8443", models=(MOE,)))
    found = pool_model_availability(pool, MOE)
    assert (found.state, found.worker) == (overflow.AVAILABLE, "10.77.0.2:8443")


def test_no_worker_advertising_the_model_is_unavailable_with_a_reason():
    pool = _Pool(_snapshot("127.0.0.1:11434", models=(DENSE,)),
                 _snapshot("10.77.0.2:8443", state="circuit_open", models=(MOE,), healthy=False))
    found = pool_model_availability(pool, MOE)
    assert found.state == overflow.UNAVAILABLE
    assert found.reason == "no eligible Ollama pool worker advertises qwen3.6:35b"


def test_unreported_worker_makes_availability_unknown():
    pool = _Pool(_snapshot("127.0.0.1:11434", models=(DENSE,)),
                 _snapshot("10.77.0.2:8443", state="unknown"))
    assert pool_model_availability(pool, MOE).state == overflow.UNKNOWN


def test_single_endpoint_uses_the_local_catalog():
    pool = _Pool(enabled=False)
    assert pool_model_availability(pool, MOE, local_has_model=lambda m: True).worker == "local Ollama"
    missing = pool_model_availability(pool, MOE, local_has_model=lambda m: False)
    assert missing.state == overflow.UNAVAILABLE
    assert pool_model_availability(pool, MOE).state == overflow.UNAVAILABLE


# -- telemetry --------------------------------------------------------------------

class _ListSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def test_overflow_emits_one_content_free_route_changed_before_the_send():
    sink = _ListSink()
    clock = iter(range(10_000))
    runtime = rt.RuntimeTelemetry(
        sink, session_id="rts-0123456789ab", version="0.9.0",
        clock=lambda: datetime(2026, 9, 27, tzinfo=timezone.utc),
        monotonic=lambda: next(clock) / 1000.0,
    )
    attempts.install_provider_attempt_observer(runtime)
    try:
        turn = runtime.begin_turn(turn_id="req-overflow", surface="http.chat_completions",
                                  stream=False, requested_model="sonder")
        with runtime.activate(turn):
            attempts.report_route_overflow(_decide().telemetry())
            attempts.dispatch_provider(
                "ollama", "/api/chat", {"model": MOE, "messages": []},
                lambda: {"model": MOE, "message": {"content": "ok"}},
            )
        runtime.finish_turn(turn, outcome="completed", http_status=200)
    finally:
        attempts.clear_provider_attempt_observer(runtime)
    codes = [event.event_code for event in sink.events]
    assert codes == ["request.started", "route.changed", "route.selected", "request.completed"]
    assert sink.events[1].fields == {
        "from_provider": "sonder_inference", "from_model": DENSE,
        "to_provider": "ollama", "to_model": MOE,
        "reason_code": "context_over_threshold",
        "estimated_tokens": 41_234, "threshold": 32_768, "attempt": 1,
    }
    assert sink.events[2].fields["model"] == MOE


def test_overflow_report_outside_a_turn_is_silent():
    sink = _ListSink()
    runtime = rt.RuntimeTelemetry(sink, session_id="rts-0123456789ab", version="0.9.0")
    attempts.install_provider_attempt_observer(runtime)
    try:
        attempts.report_route_overflow(_decide().telemetry())
    finally:
        attempts.clear_provider_attempt_observer(runtime)
    assert sink.events == []
