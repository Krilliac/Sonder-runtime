"""Unit and HTTP facade tests for the difficulty-to-execution-mode router."""
from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace
from typing import Any

from sonder_runtime.application.routing.execution_mode_service import (
    ENV_AUTO_ENSEMBLE_TIERS,
    ENV_AUTO_EXECUTION_MODE,
    ENV_FAST_TIER_LATENCY_MS,
    ENV_SLOW_TIER_LATENCY_MS,
    normalize_tier_health,
    plan_chat_execution_mode,
    policy_from_env,
)
from sonder_runtime.domain.routing.execution_mode_router import (
    DEFAULT_FAST_TIER_LATENCY_MS,
    DEFAULT_SLOW_TIER_LATENCY_MS,
    ExecutionContext,
    ExecutionDecision,
    ExecutionModePolicy,
    TierHealthHint,
    check_execution_mode,
    classify_tier_health,
    plan_execution_mode,
    route_summary_line,
    score_difficulty,
)
from sonder_runtime.interfaces.http.facades import chat_routes

_DUAL_PC_TIERS = {
    "fast": "bonsai-9b-mtp",
    "code": "bonsai-27b-pq2",
    "general": "bonsai-27b-pq2",
    "reasoning": "qwen3-coder-next-80b:latest",
}


def test_score_difficulty_bands_and_code_intent():
    band_q, score_q, _, _, _ = score_difficulty("hey sonder")
    assert band_q == "quick"
    assert score_q < 0.25

    band_s, score_s, _, _, _ = score_difficulty("Write a helper function to parse ISO dates in Python.")
    assert band_s == "standard"
    assert 0.25 <= score_s < 0.60

    band_h, score_h, signals_h, ens_mode_h, _ = score_difficulty(
        "Think hard and design a lock-free C++ ring buffer architecture; analyze memory order trade-offs and race conditions."
    )
    assert band_h == "hard"
    assert score_h >= 0.60
    assert "concurrency_memory_systems" in signals_h
    assert ens_mode_h == "prose"

    band_hc, _, _, ens_mode_hc, _ = score_difficulty(
        "Think hard and implement a lock-free SPSC queue in C++ avoiding ABA problem and data race bugs."
    )
    assert band_hc == "hard"
    assert ens_mode_hc == "code"

    band_m, score_m, _, _, mut_m = score_difficulty(
        "Audit the auth layer, scheduler, and cache in parallel from multiple angles for race conditions and threat model gaps."
    )
    assert band_m == "multi_scope"
    assert score_m >= 0.85
    assert mut_m is False

    band_a, _, _, _, mut_a = score_difficulty(
        "Implement the new routing pipeline end-to-end across frontend and backend files and fix all failing tests."
    )
    assert band_a == "multi_scope"
    assert mut_a is True


def test_plan_execution_mode_auto_routes_all_four_bands():
    auto_policy = ExecutionModePolicy(mode="auto", allow_auto_agent=False)
    full_policy = ExecutionModePolicy(mode="auto", allow_auto_agent=True)

    # Band 0 -> quick -> fast single tier
    d_quick = plan_chat_execution_mode(
        "thanks!",
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    assert d_quick.mode == "single"
    assert d_quick.band == "quick"
    assert d_quick.recommended_tiers == ("fast",)
    assert d_quick.auto_selected is True

    # Band 1 -> standard -> code single tier
    d_std = plan_chat_execution_mode(
        "Format this JSON file cleanly.",
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    assert d_std.mode == "single"
    assert d_std.band == "standard"
    assert d_std.recommended_tiers == ("code",)

    # Band 2 -> hard -> parallel Bonsai-27B + Node1 80B ensemble
    d_hard = plan_chat_execution_mode(
        "Think hard and compare the concurrency trade-offs and deadlock risks of two locking architectures.",
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    assert d_hard.mode == "ensemble"
    assert d_hard.band == "hard"
    assert d_hard.recommended_tiers == ("code", "reasoning")
    assert d_hard.auto_selected is True

    # Band 3 read-only -> local fanout
    d_fanout = plan_chat_execution_mode(
        "Audit memory safety, auth boundaries, and cache eviction across multiple modules in parallel for threat model gaps.",
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    assert d_fanout.mode == "fanout"
    assert d_fanout.band == "multi_scope"
    assert d_fanout.fanout_scope == "local"

    # Band 3 mutating -> steps down to ensemble when allow_auto_agent=False
    mut_prompt = "Implement the streaming refactor end-to-end across frontend and backend and run tests to verify."
    d_mut_safe = plan_chat_execution_mode(
        mut_prompt,
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    assert d_mut_safe.mode == "ensemble"
    assert "auto-agent disabled" in d_mut_safe.reason

    # Band 3 mutating -> routes to /agent when explicitly opted in (auto+agent)
    d_mut_agent = plan_chat_execution_mode(
        mut_prompt,
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=full_policy,
    )
    assert d_mut_agent.mode == "agent"
    assert d_mut_agent.band == "multi_scope"


def test_plan_execution_mode_guardrails_and_degradation():
    auto_policy = ExecutionModePolicy(mode="auto", allow_auto_agent=True)
    hard_prompt = "Prove this distributed consensus invariant and analyze every deadlock and race condition."

    # Non-developer never auto-triggers ensemble/fanout/agent
    d_nondev = plan_chat_execution_mode(
        hard_prompt,
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=False,
        policy=auto_policy,
    )
    assert d_nondev.mode == "single"

    # Only 1 distinct model bound (e.g., Node1 offline & everything collapsed onto 27B) -> degrades to single
    d_single_box = plan_chat_execution_mode(
        hard_prompt,
        tier_models={"code": "bonsai-27b-pq2", "reasoning": "bonsai-27b-pq2"},
        developer_authorized=True,
        policy=auto_policy,
    )
    assert d_single_box.mode == "single"
    assert "unavailable" in d_single_box.reason

    # Hard bypasses: structured_output, has_image, explicit_model_pin, requested_mode="single"
    for kwargs in (
        {"structured_output": True},
        {"has_image": True},
        {"explicit_model_pin": True},
        {"requested_mode": "single"},
    ):
        d = plan_chat_execution_mode(
            hard_prompt,
            tier_models=_DUAL_PC_TIERS,
            developer_authorized=True,
            policy=auto_policy,
            **kwargs,
        )
        assert d.mode == "single", f"expected single for {kwargs}, got {d}"
        assert d.auto_selected is False


def test_policy_from_env_and_validation():
    assert check_execution_mode("AUTO") == "auto"
    assert check_execution_mode("ensemble") == "ensemble"
    assert check_execution_mode("bogus") is None
    assert check_execution_mode(True) is None
    assert check_execution_mode(123) is None

    p_off = policy_from_env({})
    assert p_off.mode == "off"
    assert p_off.allow_auto_agent is False

    p_auto = policy_from_env({ENV_AUTO_EXECUTION_MODE: "auto"})
    assert p_auto.mode == "auto"
    assert p_auto.allow_auto_agent is False
    assert p_auto.ensemble_tiers == ("code", "reasoning")

    p_full = policy_from_env(
        {
            ENV_AUTO_EXECUTION_MODE: "auto+agent",
            ENV_AUTO_ENSEMBLE_TIERS: "fast,code,reasoning",
        }
    )
    assert p_full.mode == "auto"
    assert p_full.allow_auto_agent is True
    assert p_full.ensemble_tiers == ("fast", "code", "reasoning")

    # Direct domain decision serialization
    d = plan_execution_mode(
        ExecutionContext(
            prompt="hello",
            available_tiers=frozenset({"fast", "code"}),
            distinct_ensemble_models=2,
            distinct_local_models=2,
            developer_authorized=True,
        ),
        p_auto,
    )
    as_dict = d.to_dict()
    assert as_dict["mode"] == "single"
    assert as_dict["band"] == "quick"
    assert as_dict["recommended_tiers"] == ["fast"]


def _make_chat_harness(
    *,
    developer_authorized: bool = True,
    ensemble_calls: list[dict[str, Any]] | None = None,
    fanout_calls: list[dict[str, Any]] | None = None,
    slash_calls: list[str] | None = None,
    sent_payloads: list[tuple[int, dict[str, Any]]] | None = None,
    tier_models: dict[str, str] | None = None,
):
    payloads = sent_payloads if sent_payloads is not None else []
    ens_calls = ensemble_calls if ensemble_calls is not None else []
    fan_calls = fanout_calls if fanout_calls is not None else []
    sl_calls = slash_calls if slash_calls is not None else []

    class FakeAdmissionRejected(Exception):
        code = "busy"
        status = 503
        retryable = True

    class FakeModelCallError(Exception):
        kind = "backend"
        status = 502
        detail = "boom"
        cloud = False
        retry_after_seconds = None

    class FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    state = SimpleNamespace(
        lock=FakeLock(),
        persona=None,
        last_iid="",
        last_run_source="",
        last_response="",
    )

    class FakeContextVar:
        def set(self, _v):
            return "tok"

        def reset(self, _tok):
            pass

    class FakeHandler:
        headers: dict[str, str] = {}
        client_address = ("127.0.0.1", 12345)
        _request_started = 0.0
        _telemetry_error_reply = False

        def _correlation(self) -> str:
            return "corr-test-1"

        def _send_json_payload(self, obj: dict[str, Any], status: int = 200, headers: Any = None) -> None:
            payloads.append((status, obj))

        def _send_json(self, obj: dict[str, Any], elapsed_ms: Any = None) -> None:
            payloads.append((200, obj))

        def _record_chat_completion_metric(self, *_a, **_kw) -> None:
            pass

        def _begin_chat_turn_telemetry(self, **_kw) -> None:
            pass

        def log_error(self, *_a, **_kw) -> None:
            pass

        def _end_early_stream_with_error(self, *_a, **_kw) -> bool:
            return False

        def _begin_early_stream(self, *_a, **_kw) -> None:
            pass

        def _pause_early_stream(self) -> None:
            pass

        def _run_streamable_prompt(self, prompt: str, history: Any, selector: Any, **_kw) -> Any:
            return SimpleNamespace(
                content=f"single:{selector or 'default'}:{prompt}",
                iid="iid-1",
                thinking="",
                resolved_model="bonsai-9b-mtp" if selector == "fast" else "bonsai-27b-pq2",
                resolved_tier=selector or "code",
                cache="",
                provider_capture=None,
                run_source="chat",
            )

    runtime = SimpleNamespace(
        ModelCallError=FakeModelCallError,
        _format_model_call_error=str,
        _open_db=lambda: SimpleNamespace(close=lambda: None),
        natural_model_request=lambda _p: None,
        activity_tracker=SimpleNamespace(
            span=lambda *_a, **_kw: contextlib.nullcontext(),
            response_span=lambda *_a, **_kw: contextlib.nullcontext(None),
            current_response=lambda: None,
            current_reasoning=lambda: "",
        ),
        prewarm_model=lambda _s: None,
        _http_chat_tools=lambda: SimpleNamespace(
            negotiate_client_exec=lambda *_a, **_kw: (None, None),
            resolve_client_slash_for_request=lambda *_a, **_kw: None,
            account_default_project=lambda *_a: "default",
            build_http_chat_tool_turn=lambda **_kw: None,
        ),
        route_computer_use=lambda _p: None,
        chat_web_response=lambda *_a, **_kw: None,
        served_control_guard=lambda _g: contextlib.nullcontext(),
        _turn_activity_response=lambda *_a, **_kw: None,
    )

    serve_mod = SimpleNamespace(
        server=runtime,
        _serve_logger=logging.getLogger("test_execution_mode_router"),
        InvalidInput=ValueError,
        PUBLIC_HOSTING=False,
        _SPANDA_CONFIG=None,
        admin_auth=SimpleNamespace(role_allows=lambda *_a: True),
        permission_policy=SimpleNamespace(mode_scope=lambda *_a: contextlib.nullcontext()),
        _CHAT_HISTORY_SOURCES=("auto", "client"),
        _CHAT_REFUSALS=FakeContextVar(),
        _CLIENT_TOOL_BROKER=None,
        _chat_tool_stream=None,
        _chunk=lambda *_a: b"",
        intents=None,
        _validate_chat_messages=lambda m: list(m or []),
        _last_user_message=lambda m: (m[-1].get("content", "") if m else ""),
        _parse_natural_model_selection=lambda *_a, **_kw: None,
        _response_format_schema=lambda _r: None,
        _uses_default_model_route=lambda m: m in ("", "sonder", "default"),
        _restricted_tool_role=lambda _c: None,
        _dangerous_http_slash=lambda _p: False,
        _developer_authorized=lambda _c: developer_authorized,
        _request_model_selector=lambda m: "" if m in ("", "sonder", "default") else m,
        _chat_model_selection_error=lambda _s: None,
        _normalize_session_id=lambda s, **_kw: str(s or "default"),
        _hosted_storage_id=lambda _c, s, _k="": str(s or "default"),
        _http_scope_value=lambda v, _k: str(v or ""),
        _storage_session_id=lambda _c, s: s,
        _storage_project=lambda _c, p: str(p or ""),
        _work_project_for_request=lambda *_a, **_kw: "",
        _history_from_messages=lambda _m: [],
        _server_side_history=lambda _s: [],
        _served_chat_location_hint=lambda _r: ("", False),
        _request_lifecycle=lambda: SimpleNamespace(metrics=None),
        _require_http_session_state_owner=lambda *_a, **_kw: None,
        _acquire_http_conversation_state=lambda *_a, **_kw: (state, False),
        _request_account_token=lambda *_a, **_kw: "",
        _release_http_conversation_state=lambda *_a, **_kw: None,
        _ensure_refusal_observer=lambda: None,
        _feed_request_owner=lambda _c: "owner",
        _task_account_scope=lambda _c: None,
        _state_principal=lambda _c: "owner",
        _mode_principal=lambda _c: "owner",
        _reasoning_request_owner=lambda _c: "owner",
        _reasoning_visible_to=lambda _c: False,
        _served_control_guard_for=lambda *_a, **_kw: None,
        _handle_slash=lambda p, **_kw: (
            (sl_calls.append(p) or f"slash:{p}")
            if str(p or "").lstrip().startswith("/")
            else None
        ),
        _handle_work_intent=lambda *_a, **_kw: None,
        _handle_feedback=lambda *_a, **_kw: None,
        _handle_intent=lambda *_a, **_kw: None,
        _handle_natural_command=lambda *_a, **_kw: None,
        _model_to_tier=lambda _m: "code",
        _is_loopback_host=lambda _h: True,
        _http_server_location_lookup_allowed=lambda _c: False,
        _record_chat=lambda *_a, **_kw: None,
        _capture_live_session_turn=lambda **_kw: None,
        _append_session_transcript_event=lambda *_a, **_kw: None,
        _is_legacy_error_reply=lambda _c: False,
        _request_cache_scope=lambda _c: "",
        _chat_completion_object=lambda content, model, **kw: {
            "model": model,
            "choices": [{"message": {"content": content}}],
            "sonder_receipt": kw.get("receipt"),
        },
        _activity_response=lambda *_a, **_kw: None,
        _turn_activity=lambda *_a, **_kw: None,
    )

    deps = chat_routes.ChatTurnDeps(
        lifecycle_mod=SimpleNamespace(
            AdmissionRejected=FakeAdmissionRejected,
            get=lambda: SimpleNamespace(metrics=None),
        ),
        context_policy_parse_strict=lambda v: v,
        chat_temperature_mod=SimpleNamespace(
            session_field=lambda *_a: (False, None),
            store_session_field=lambda *_a: None,
            resolve_turn_temperature=lambda **_kw: None,
        ),
        chat_effort_mod=SimpleNamespace(
            check_effort=lambda v: v,
            session_field=lambda *_a: (False, None),
            store_session_field=lambda *_a: None,
            resolve_turn_effort=lambda **_kw: SimpleNamespace(value="off"),
            effort_scope=lambda _v: contextlib.nullcontext(),
        ),
        personas_mod=SimpleNamespace(
            AUTO="auto",
            MODES=("auto",),
            names=lambda: [],
            normalize_selection=lambda v: v,
            resolve_persona=lambda *_a, **_kw: SimpleNamespace(name="default"),
            persona_scope=lambda _v: contextlib.nullcontext(),
        ),
        image_generation_mod=SimpleNamespace(
            strip_inline_data_images=lambda s: s,
            is_image_generation_model_name=lambda _m: False,
        ),
        model3d_generation_mod=SimpleNamespace(),
        thinking_controls_mod=SimpleNamespace(
            REPLY_TRUNCATION=FakeContextVar(),
            begin_reply_truncation=lambda: ([False], "tok"),
        ),
        reference_lookup_lanes_mod=SimpleNamespace(
            reset_turn=lambda: None,
            finish_http=lambda content, _receipt: content,
        ),
        chat_formatting_mod=SimpleNamespace(chat_usage=lambda _a: None),
        load_spanda_http=lambda: SimpleNamespace(
            resolve_spanda_policy=lambda *_a, **_kw: SimpleNamespace(
                active=False, block=False, samples=1, temperature=None, reason="off",
            ),
            should_run_spanda=lambda *_a, **_kw: False,
        ),
        acquire_chat_slot=lambda _lc: contextlib.nullcontext(),
        run_natural_fanout=lambda nm: (fan_calls.append(nm) or "fanout-reply"),
        run_natural_ensemble=lambda nm, proj: (ens_calls.append({**nm, "project": proj}) or "ensemble-reply"),
        resolve_local_tier_models=lambda: dict(_DUAL_PC_TIERS if tier_models is None else tier_models),
    )

    return FakeHandler(), serve_mod, deps, payloads, ens_calls, fan_calls, sl_calls


def test_http_chat_routes_auto_ensemble_and_validation(monkeypatch):
    monkeypatch.setenv(ENV_AUTO_EXECUTION_MODE, "auto")
    monkeypatch.setattr(chat_routes, "run_chat_media_route", lambda **_kw: (None, None, None))
    monkeypatch.setattr(chat_routes, "rate_limit_and_meter", lambda *_a, **_kw: (True, None))

    # 1. Invalid execution_mode -> HTTP 400
    handler, serve_mod, deps, payloads, _, _, _ = _make_chat_harness()
    chat_routes.handle_chat_completions(
        handler,
        serve_mod,
        {"model": "sonder", "messages": [{"role": "user", "content": "hi"}], "execution_mode": "invalid_xyz"},
        {"role": "admin", "authorized": True, "account": None},
        model_operation="chat",
        request_temperature=None,
        facade=None,
        record_early_chat_metric=lambda _k: None,
        deps=deps,
    )
    assert payloads[-1][0] == 400
    assert "execution_mode" in payloads[-1][1]["error"]["message"]

    # 2. Non-developer requesting explicit ensemble -> HTTP 403
    handler, serve_mod, deps, payloads, _, _, _ = _make_chat_harness(developer_authorized=False)
    chat_routes.handle_chat_completions(
        handler,
        serve_mod,
        {"model": "sonder", "messages": [{"role": "user", "content": "hi"}], "execution_mode": "ensemble"},
        {"role": "viewer", "authorized": True, "account": None},
        model_operation="chat",
        request_temperature=None,
        facade=None,
        record_early_chat_metric=lambda _k: None,
        deps=deps,
    )
    assert payloads[-1][0] == 403

    # 3. Hard reasoning prompt -> auto-routes to Bonsai + 80B ensemble with receipt metadata
    handler, serve_mod, deps, payloads, ens_calls, _, _ = _make_chat_harness()
    chat_routes.handle_chat_completions(
        handler,
        serve_mod,
        {
            "model": "sonder",
            "messages": [
                {
                    "role": "user",
                    "content": "Think hard and analyze deadlock and race condition trade-offs in this distributed architecture.",
                }
            ],
        },
        {"role": "admin", "authorized": True, "account": None},
        model_operation="chat",
        request_temperature=None,
        facade=None,
        record_early_chat_metric=lambda _k: None,
        deps=deps,
    )
    assert payloads[-1][0] == 200
    assert len(ens_calls) == 1
    assert ens_calls[0]["tiers"] == "code,reasoning"
    assert ens_calls[0]["mode"] == "prose"
    assert ens_calls[0]["require_all_tiers"] is False
    receipt = payloads[-1][1]["sonder_receipt"]
    assert receipt["execution_route"]["mode"] == "ensemble"
    assert receipt["execution_route"]["band"] == "hard"
    assert receipt["execution_route"]["auto_selected"] is True

    # 4. Multi-scope read-only prompt -> auto-routes to local fanout
    handler, serve_mod, deps, payloads, _, fan_calls, _ = _make_chat_harness()
    chat_routes.handle_chat_completions(
        handler,
        serve_mod,
        {
            "model": "sonder",
            "messages": [
                {
                    "role": "user",
                    "content": "Audit auth, cache, and scheduling across multiple modules in parallel for threat model and race condition gaps.",
                }
            ],
        },
        {"role": "admin", "authorized": True, "account": None},
        model_operation="chat",
        request_temperature=None,
        facade=None,
        record_early_chat_metric=lambda _k: None,
        deps=deps,
    )
    assert payloads[-1][0] == 200
    assert len(fan_calls) == 1
    assert fan_calls[0]["scope"] == "local"
    assert payloads[-1][1]["sonder_receipt"]["execution_route"]["mode"] == "fanout"

    # 5. Explicit execution_mode="single" overrides auto-ensemble
    handler, serve_mod, deps, payloads, ens_calls, _, _ = _make_chat_harness()
    chat_routes.handle_chat_completions(
        handler,
        serve_mod,
        {
            "model": "sonder",
            "execution_mode": "single",
            "messages": [
                {
                    "role": "user",
                    "content": "Think hard and analyze deadlock and race condition trade-offs.",
                }
            ],
        },
        {"role": "admin", "authorized": True, "account": None},
        model_operation="chat",
        request_temperature=None,
        facade=None,
        record_early_chat_metric=lambda _k: None,
        deps=deps,
    )
    assert payloads[-1][0] == 200
    assert ens_calls == []
    assert payloads[-1][1]["sonder_receipt"]["execution_route"]["mode"] == "single"

    # 6. Quick prompt -> auto-routes to fast tier on single model
    handler, serve_mod, deps, payloads, ens_calls, _, _ = _make_chat_harness()
    chat_routes.handle_chat_completions(
        handler,
        serve_mod,
        {"model": "sonder", "messages": [{"role": "user", "content": "hey sonder"}]},
        {"role": "admin", "authorized": True, "account": None},
        model_operation="chat",
        request_temperature=None,
        facade=None,
        record_early_chat_metric=lambda _k: None,
        deps=deps,
    )
    assert payloads[-1][0] == 200
    assert ens_calls == []
    assert payloads[-1][1]["choices"][0]["message"]["content"].startswith("single:fast:")
    assert payloads[-1][1]["sonder_receipt"]["execution_route"]["band"] == "quick"

    # 7. Opt-in auto+agent -> routes multi-scope mutating task to /agent
    monkeypatch.setenv(ENV_AUTO_EXECUTION_MODE, "auto+agent")
    handler, serve_mod, deps, payloads, ens_calls, _, sl_calls = _make_chat_harness()
    chat_routes.handle_chat_completions(
        handler,
        serve_mod,
        {
            "model": "sonder",
            "messages": [
                {
                    "role": "user",
                    "content": "Implement the new cache eviction policy end-to-end across frontend and backend and fix all failing tests.",
                }
            ],
        },
        {"role": "admin", "authorized": True, "account": None},
        model_operation="chat",
        request_temperature=None,
        facade=None,
        record_early_chat_metric=lambda _k: None,
        deps=deps,
    )
    assert payloads[-1][0] == 200
    assert len(sl_calls) == 1
    assert sl_calls[0].startswith("/agent ")
    assert payloads[-1][1]["sonder_receipt"]["execution_route"]["mode"] == "agent"


def test_domain_calibration_code_audit_math_architecture_and_debugging():
    # Code audit + subtle bug signals -> hard
    band_audit, score_audit, sig_audit, _, _ = score_difficulty(
        "Perform a code audit on this router module to find subtle bugs and edge cases."
    )
    assert band_audit == "hard"
    assert score_audit >= 0.60
    assert "code_audit" in sig_audit

    # Pure math/proof with deep multi-concept density -> hard
    band_math, score_math, sig_math, _, _ = score_difficulty(
        "Prove by induction that the recurrence T(n) = 2T(n/2) + n has asymptotic time complexity "
        "and provide a counterexample when the base case is dropped."
    )
    assert band_math == "hard"
    assert score_math >= 0.60
    assert "formal_math_proof" in sig_math
    assert "formal_math_proof:deep" in sig_math

    # Single-concept math definition lookup -> stays standard (no false escalation)
    band_math_simple, _, _, _, _ = score_difficulty("What is asymptotic complexity?")
    assert band_math_simple == "standard"

    # Rich architecture design -> hard
    band_arch, score_arch, sig_arch, _, _ = score_difficulty(
        "Design a distributed scheduler architecture with backpressure, idempotency, invariants, and failure modes."
    )
    assert band_arch == "hard"
    assert score_arch >= 0.60
    assert "architecture_tradeoff" in sig_arch
    assert "architecture_tradeoff:deep" in sig_arch

    # Deep crash/non-deterministic debugging -> hard
    band_dbg, score_dbg, sig_dbg, _, _ = score_difficulty(
        "Diagnose this non-deterministic segfault and access violation 0xC0000005 with a minimal reproducer."
    )
    assert band_dbg == "hard"
    assert score_dbg >= 0.60
    assert "deep_debug_crash" in sig_dbg
    assert "root_cause_debug" in sig_dbg

    # Simple casual "why does" question -> stays standard
    band_casual, score_casual, _, _, _ = score_difficulty("Why does print() add a newline in Python?")
    assert band_casual == "standard"
    assert score_casual < 0.60


def test_multi_file_signals_and_url_isolation():
    # 2 distinct file references + audit cue -> hard
    band_two, score_two, sig_two, _, mut_two = score_difficulty(
        "Review sonder_runtime/domain/routing/execution_mode_router.py and "
        "sonder_runtime/application/routing/execution_mode_service.py for subtle bugs and regressions."
    )
    assert band_two == "hard"
    assert score_two >= 0.60
    assert "multi_file_refs:2" in sig_two
    assert mut_two is False

    # 3 distinct Windows backslash file paths + read-only audit -> multi_scope (non-mutating -> fanout)
    band_three, _, sig_three, _, mut_three = score_difficulty(
        r"Audit sonder_runtime\domain\routing\execution_mode_router.py, "
        r"sonder_runtime\application\routing\execution_mode_service.py, and "
        r"tests\test_execution_mode_router.py for race conditions and threat model gaps."
    )
    assert band_three == "multi_scope"
    assert "multi_file_refs:3" in sig_three
    assert mut_three is False

    # 3 distinct file paths + mutating verb -> multi_scope + wants_mutating_agent
    band_mut, _, sig_mut, _, mut_flag = score_difficulty(
        "Refactor foo.py, bar.py, and baz.py to support tier health fallbacks."
    )
    assert band_mut == "multi_scope"
    assert "multi_file_refs:3" in sig_mut
    assert mut_flag is True

    # Same file repeated with mixed separators/casing counts as 1 file, not 3
    band_dup, _, sig_dup, _, mut_dup = score_difficulty(
        r"Check src/router.py, SRC\router.py, and src/ROUTER.py for formatting."
    )
    assert band_dup == "standard"
    assert not any(s.startswith("multi_file_refs:") for s in sig_dup)
    assert mut_dup is False

    # URLs containing .py and query '?' must not trigger multi_file or multi_question
    band_url, _, sig_url, _, _ = score_difficulty(
        "Fetch https://example.com/a.py?x=1?y=2 and https://example.com/b.py?z=3?w=4 and summarize."
    )
    assert band_url == "standard"
    assert not any(s.startswith("multi_question:") for s in sig_url)
    assert not any(s.startswith("multi_file_refs:") for s in sig_url)


def test_pasted_logs_and_dumps_avoid_false_escalation():
    auto_policy = ExecutionModePolicy(mode="auto", allow_auto_agent=True)

    # 1. Unfenced timestamped server logs full of trap keywords, bullets, and '?' query strings
    noisy_log = "\n".join(
        [
            "Summarize what failed in this log:",
            "2026-11-25 01:00:01 INFO scheduler started across multiple modules in parallel",
            "2026-11-25 01:00:02 WARN potential deadlock and race condition in distributed lock",
            "2026-11-25 01:00:03 ERROR GET /api?a=1&b=2?c=3?d=4 failed threat model check",
            "2026-11-25 01:00:04 INFO - item 1: memory leak probe ok",
            "2026-11-25 01:00:05 INFO - item 2: proof verification skipped",
            "2026-11-25 01:00:06 ERROR - item 3: upstream timeout after 30s",
        ]
    )
    d_log = plan_chat_execution_mode(
        noisy_log,
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    assert d_log.band == "standard"
    assert d_log.mode == "single"
    assert "pasted_log_dampened" in d_log.signals

    # 2. Large fenced log block with brief question
    fenced_log = (
        "What HTTP status code was returned?\n"
        "```log\n"
        + "\n".join(
            f"[2026-11-25 01:01:{i:02d}] ERROR deadlock race condition distributed?q={i}"
            for i in range(25)
        )
        + "\n```"
    )
    d_fenced = plan_chat_execution_mode(
        fenced_log,
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    assert d_fenced.band == "standard"
    assert d_fenced.mode == "single"
    assert "pasted_log_dampened" in d_fenced.signals

    # 3. Raw pasted Python traceback with zero surrounding prompt -> standard, not escalated
    raw_tb = (
        "Traceback (most recent call last):\n"
        '  File "sonder_runtime/serve.py", line 120, in dispatch\n'
        "    return run_deadlock_check(a, b, c)\n"
        '  File "sonder_runtime/worker.py", line 44, in run_deadlock_check\n'
        "    raise RuntimeError('distributed race condition')\n"
        "RuntimeError: distributed race condition"
    )
    band_tb, score_tb, sig_tb, _, _ = score_difficulty(raw_tb)
    assert band_tb == "standard"
    assert score_tb == 0.35
    assert sig_tb == ("pasted_log_only",)

    # 4. Compiler error output + simple question -> standard
    msvc_errors = (
        "Explain the first compiler error:\n"
        "src/foo.cpp(42): error C2065: 'deadlock_guard': undeclared identifier\n"
        "src/foo.cpp(45): error C2065: 'race_state': undeclared identifier\n"
        "src/foo.cpp(49): warning C4100: unreferenced formal parameter\n"
    )
    band_cc, _, sig_cc, _, _ = score_difficulty(msvc_errors)
    assert band_cc == "standard"
    assert "pasted_log_dampened" in sig_cc

    # 5. NDJSON log dump + simple extraction ask -> standard
    ndjson_prompt = (
        "Count the error entries:\n"
        '{"timestamp":"2026-11-25T01:00:01Z","level":"INFO","msg":"boot"}\n'
        '{"timestamp":"2026-11-25T01:00:02Z","level":"ERROR","msg":"deadlock risk"}\n'
        '{"timestamp":"2026-11-25T01:00:03Z","level":"ERROR","msg":"race condition"}\n'
    )
    band_ndjson, _, sig_ndjson, _, _ = score_difficulty(ndjson_prompt)
    assert band_ndjson == "standard"
    assert "pasted_log_dampened" in sig_ndjson

    # 6. Genuine hard prompt WITH an attached log still routes to hard ensemble (not fanout!)
    hard_with_log = (
        "Think hard and diagnose the root cause of this intermittent deadlock and memory order race condition:\n"
        "2026-11-25 01:00:01 ERROR worker-1 stalled on lock acquisition\n"
        "2026-11-25 01:00:02 ERROR worker-2 stalled on lock acquisition\n"
        "2026-11-25 01:00:03 ERROR worker-3 stalled on lock acquisition\n"
        "2026-11-25 01:00:04 WARN - bullet 1\n"
        "2026-11-25 01:00:05 WARN - bullet 2\n"
        "2026-11-25 01:00:06 WARN - bullet 3\n"
    )
    d_hard_log = plan_chat_execution_mode(
        hard_with_log,
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    assert d_hard_log.band == "hard"
    assert d_hard_log.mode == "ensemble"
    assert "pasted_log_dampened" in d_hard_log.signals

    # 7. Fenced code block + fix verb sets ensemble_mode="code"
    code_fix_prompt = (
        "Think hard and fix the race condition and deadlock in this snippet:\n"
        "```python\n"
        "def run(lock_a, lock_b):\n"
        "    with lock_a:\n"
        "        with lock_b:\n"
        "            return 1\n"
        "```"
    )
    band_cf, _, _, ens_cf, _ = score_difficulty(code_fix_prompt)
    assert band_cf == "hard"
    assert ens_cf == "code"


def test_conversation_depth_and_prompt_length_calibration():
    auto_policy = ExecutionModePolicy(mode="auto", allow_auto_agent=False)

    # Pure greetings stay quick even in deep conversations
    for greeting in ("thanks!", "ok", "hey sonder", "cool"):
        d = plan_chat_execution_mode(
            greeting,
            tier_models=_DUAL_PC_TIERS,
            developer_authorized=True,
            conversation_depth=15,
            policy=auto_policy,
        )
        assert d.band == "quick", f"expected quick for {greeting!r}, got {d}"
        assert d.recommended_tiers == ("fast",)

    # Short non-greeting follow-up in a moderate-depth thread avoids dropping to quick
    band_shallow, _, _, _, _ = score_difficulty("What about edge cases?", conversation_depth=0)
    band_mid, _, sig_mid, _, _ = score_difficulty("What about edge cases?", conversation_depth=5)
    assert band_shallow == "quick"
    assert band_mid == "standard"
    assert "deep_conversation:moderate" in sig_mid

    # Borderline math derivation escalates from standard to hard in a deep conversation
    borderline = "Derive the recurrence bound for the variant we just discussed."
    band_0, score_0, _, _, _ = score_difficulty(borderline, conversation_depth=0)
    band_12, score_12, sig_12, _, _ = score_difficulty(borderline, conversation_depth=12)
    assert band_0 == "standard"
    assert score_0 < 0.60
    assert band_12 == "hard"
    assert score_12 >= 0.60
    assert "deep_conversation:high" in sig_12

    # Very long plain prose with zero domain difficulty cues stays capped in standard
    plain_long = "Rewrite the following draft note to be friendlier: " + ("hello team we met today. " * 65)
    band_long, score_long, sig_long, _, _ = score_difficulty(plain_long, conversation_depth=12)
    assert "very_long_prompt" in sig_long
    assert band_long == "standard"
    assert score_long <= 0.55

    # Long prompt WITH a real debugging signal escalates to hard
    long_debug = (
        "Diagnose the root cause of this regression in our streaming consumer: "
        + ("when the client sends a partial frame under load the buffer index drifts. " * 10)
    )
    band_ld, score_ld, sig_ld, _, _ = score_difficulty(long_debug, conversation_depth=10)
    assert band_ld == "hard"
    assert score_ld >= 0.60
    assert "long_prompt" in sig_ld
    assert "root_cause_debug" in sig_ld

    # Negative and boolean conversation_depth values degrade safely to 0
    band_neg, _, _, _, _ = score_difficulty("thanks!", conversation_depth=-5)
    assert band_neg == "quick"


def test_health_and_latency_aware_tier_fallbacks():
    auto_policy = ExecutionModePolicy(mode="auto", allow_auto_agent=False)
    hard_prompt = "Think hard and analyze deadlock and race condition trade-offs in this distributed architecture."

    # 1. classify_tier_health edge cases
    assert classify_tier_health(TierHealthHint("fast", latency_ms=500.0), auto_policy) == "healthy"
    assert classify_tier_health(TierHealthHint("fast", latency_ms=5000.0), auto_policy) == "slow"
    assert classify_tier_health(TierHealthHint("reasoning", latency_ms=5000.0), auto_policy) == "healthy"
    assert classify_tier_health(TierHealthHint("reasoning", latency_ms=16000.0), auto_policy) == "slow"
    assert classify_tier_health(
        TierHealthHint("reasoning", latency_ms=3000.0, slow_threshold_ms=2000.0), auto_policy
    ) == "slow"
    assert classify_tier_health(TierHealthHint("code", available=False), auto_policy) == "unavailable"
    assert classify_tier_health(TierHealthHint("code", healthy=False), auto_policy) == "unavailable"
    assert classify_tier_health(TierHealthHint("code", error_rate=0.6), auto_policy) == "unavailable"
    assert classify_tier_health(TierHealthHint("code", error_rate=float("nan")), auto_policy) == "unavailable"
    assert classify_tier_health(TierHealthHint("code", latency_ms=float("inf")), auto_policy) == "slow"

    # 2. Quick prompt when fast tier is slow -> reroutes to healthy general, keeps fast as late fallback
    d_fast_slow = plan_chat_execution_mode(
        "hey sonder",
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        tier_latency_ms={"fast": 6500.0, "general": 800.0, "code": 900.0},
        policy=auto_policy,
    )
    assert d_fast_slow.mode == "single"
    assert d_fast_slow.band == "quick"
    assert d_fast_slow.recommended_tiers == ("general",)
    assert d_fast_slow.fallback_tiers[-1] == "fast"
    assert d_fast_slow.degraded_tiers == ("fast",)
    assert d_fast_slow.health_degraded is True
    assert "fast slow" in d_fast_slow.reason

    # 3. Quick prompt when ALL tiers are slow -> picks lowest-latency slow tier, orders fallbacks ascending
    d_all_slow = plan_chat_execution_mode(
        "hey sonder",
        tier_models={"fast": "m1", "general": "m2", "code": "m3"},
        developer_authorized=True,
        tier_latency_ms={"fast": 7000.0, "general": 22000.0, "code": 17000.0},
        policy=auto_policy,
    )
    assert d_all_slow.mode == "single"
    assert d_all_slow.recommended_tiers == ("fast",)
    assert d_all_slow.fallback_tiers == ("code", "general")
    assert d_all_slow.health_degraded is True

    # 4. Standard prompt when code is unavailable -> falls back to general
    d_code_down = plan_chat_execution_mode(
        "Format this JSON file cleanly.",
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        unavailable_tiers={"code"},
        policy=auto_policy,
    )
    assert d_code_down.mode == "single"
    assert d_code_down.recommended_tiers == ("general",)
    assert "code" not in d_code_down.fallback_tiers
    assert d_code_down.degraded_tiers == ("code",)
    assert d_code_down.health_degraded is True

    # 5. Hard prompt when reasoning is offline and only 1 distinct healthy model remains -> degrades to single
    d_node1_down_single = plan_chat_execution_mode(
        hard_prompt,
        tier_models={
            "code": "bonsai-27b-pq2",
            "general": "bonsai-27b-pq2",
            "reasoning": "qwen3-coder-next-80b:latest",
        },
        developer_authorized=True,
        tier_health={"reasoning": {"available": False, "reason": "Node1 unreachable"}},
        policy=auto_policy,
    )
    assert d_node1_down_single.mode == "single"
    assert d_node1_down_single.recommended_tiers == ("code",)
    assert d_node1_down_single.fallback_tiers == ("general",)
    assert d_node1_down_single.degraded_tiers == ("reasoning",)
    assert d_node1_down_single.health_degraded is True
    assert "Node1 unreachable" in d_node1_down_single.reason

    # 6. Hard prompt when reasoning is offline but fast (9B) + code (27B) are distinct healthy models -> ensemble[code, fast]
    d_node1_down_fallback_ens = plan_chat_execution_mode(
        hard_prompt,
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        unavailable_tiers={"reasoning"},
        policy=auto_policy,
    )
    assert d_node1_down_fallback_ens.mode == "ensemble"
    assert d_node1_down_fallback_ens.recommended_tiers == ("code", "fast")
    assert d_node1_down_fallback_ens.degraded_tiers == ("reasoning",)
    assert d_node1_down_fallback_ens.health_degraded is True
    assert "bypassed reasoning unavailable" in d_node1_down_fallback_ens.reason

    # 7. Hard prompt when reasoning is slow (25s) with only code+general+reasoning:
    # degrade_on_slow_tier=True degrades to single (keeping reasoning in fallback_tiers);
    # degrade_on_slow_tier=False keeps ensemble and annotates degraded (not bypassed).
    three_tiers = {
        "code": "bonsai-27b-pq2",
        "general": "bonsai-27b-pq2",
        "reasoning": "qwen3-coder-next-80b:latest",
    }
    d_slow_degrade = plan_chat_execution_mode(
        hard_prompt,
        tier_models=three_tiers,
        developer_authorized=True,
        tier_latency_ms={"code": 900.0, "general": 950.0, "reasoning": 25000.0},
        policy=ExecutionModePolicy(mode="auto", degrade_on_slow_tier=True),
    )
    assert d_slow_degrade.mode == "single"
    assert d_slow_degrade.recommended_tiers == ("code",)
    assert d_slow_degrade.fallback_tiers == ("general", "reasoning")
    assert d_slow_degrade.degraded_tiers == ("reasoning",)
    assert d_slow_degrade.health_degraded is True
    assert "25000ms" in d_slow_degrade.reason

    d_slow_keep = plan_chat_execution_mode(
        hard_prompt,
        tier_models=three_tiers,
        developer_authorized=True,
        tier_latency_ms={"code": 900.0, "general": 950.0, "reasoning": 25000.0},
        policy=ExecutionModePolicy(mode="auto", degrade_on_slow_tier=False),
    )
    assert d_slow_keep.mode == "ensemble"
    assert d_slow_keep.recommended_tiers == ("code", "reasoning")
    assert d_slow_keep.degraded_tiers == ("reasoning",)
    assert d_slow_keep.health_degraded is True
    assert "degraded reasoning slow" in d_slow_keep.reason
    assert "bypassed" not in d_slow_keep.reason

    # 8. Unrelated tier (fast) unavailable does NOT mark hard ensemble[code+reasoning] as degraded
    d_unrelated_down = plan_chat_execution_mode(
        hard_prompt,
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        unavailable_tiers={"fast"},
        policy=auto_policy,
    )
    assert d_unrelated_down.mode == "ensemble"
    assert d_unrelated_down.recommended_tiers == ("code", "reasoning")
    assert d_unrelated_down.health_degraded is False
    assert d_unrelated_down.degraded_tiers == ()
    assert "fast" not in d_unrelated_down.fallback_tiers

    # 9. All tiers unavailable -> never crashes, returns best-effort single with empty fallbacks
    d_all_down = plan_chat_execution_mode(
        hard_prompt,
        tier_models={"code": "m1", "reasoning": "m2"},
        developer_authorized=True,
        unavailable_tiers={"code", "reasoning"},
        policy=auto_policy,
    )
    assert d_all_down.mode == "single"
    assert d_all_down.recommended_tiers == ("reasoning",)
    assert d_all_down.fallback_tiers == ()
    assert d_all_down.health_degraded is True

    # 10. Direct domain plan_execution_mode without tier_models still degrades when reasoning is unavailable
    d_direct = plan_execution_mode(
        ExecutionContext(
            prompt=hard_prompt,
            available_tiers=frozenset({"code", "reasoning"}),
            distinct_ensemble_models=2,
            distinct_local_models=2,
            developer_authorized=True,
            tier_health=(TierHealthHint("reasoning", available=False, reason="socket timeout"),),
        ),
        auto_policy,
    )
    assert d_direct.mode == "single"
    assert d_direct.recommended_tiers == ("code",)
    assert d_direct.degraded_tiers == ("reasoning",)
    assert d_direct.health_degraded is True
    assert "socket timeout" in d_direct.reason

    # 11. Image input falls back from unavailable vision tier to general
    d_img = plan_execution_mode(
        ExecutionContext(
            prompt="describe this diagram",
            available_tiers=frozenset({"vision", "general"}),
            has_image=True,
            tier_health=(TierHealthHint("vision", available=False),),
        ),
        auto_policy,
    )
    assert d_img.mode == "single"
    assert d_img.recommended_tiers == ("general",)
    assert d_img.degraded_tiers == ("vision",)
    assert d_img.health_degraded is True


def test_normalize_tier_health_and_env_latency_thresholds():
    hints = normalize_tier_health(
        {"Reasoning": {"healthy": True, "error_rate": 0.1, "reason": "warm"}},
        tier_latency_ms={"reasoning": 1200.0, "fast": 350.0, "bad": -5.0},
        unavailable_tiers=["code"],
    )
    by_tier = {h.tier: h for h in hints}
    assert sorted(by_tier.keys()) == ["code", "fast", "reasoning"]
    assert by_tier["code"].available is False
    assert by_tier["code"].healthy is False
    assert by_tier["fast"].latency_ms == 350.0
    assert by_tier["reasoning"].latency_ms == 1200.0
    assert by_tier["reasoning"].error_rate == 0.1
    assert by_tier["reasoning"].reason == "warm"

    p_custom = policy_from_env(
        {
            ENV_AUTO_EXECUTION_MODE: "auto",
            ENV_SLOW_TIER_LATENCY_MS: "8000",
            ENV_FAST_TIER_LATENCY_MS: "1500",
        }
    )
    assert p_custom.slow_tier_latency_ms == 8000.0
    assert p_custom.fast_tier_latency_ms == 1500.0

    p_invalid = policy_from_env(
        {
            ENV_AUTO_EXECUTION_MODE: "auto",
            ENV_SLOW_TIER_LATENCY_MS: "-100",
            ENV_FAST_TIER_LATENCY_MS: "not-a-number",
        }
    )
    assert p_invalid.slow_tier_latency_ms == DEFAULT_SLOW_TIER_LATENCY_MS
    assert p_invalid.fast_tier_latency_ms == DEFAULT_FAST_TIER_LATENCY_MS


def test_route_summary_line_formats_decisions_and_receipt_dicts():
    auto_policy = ExecutionModePolicy(mode="auto", allow_auto_agent=False)

    d_quick = plan_chat_execution_mode(
        "hey sonder",
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    assert route_summary_line(d_quick) == "auto | quick (0.12) | single[fast]"
    assert route_summary_line(d_quick.to_dict()) == route_summary_line(d_quick)
    assert d_quick.to_dict()["summary"] == route_summary_line(d_quick)

    d_code_ens = plan_chat_execution_mode(
        "Think hard and implement a lock-free SPSC queue in C++ avoiding ABA problem and data race bugs.",
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    code_summary = route_summary_line(d_code_ens, include_signals=True, max_signals=2)
    assert code_summary.startswith("auto | hard (")
    assert "| ensemble:code[code+reasoning] |" in code_summary
    assert "| signals:" in code_summary
    assert "+" in code_summary.split("| signals:")[1]
    assert route_summary_line(d_code_ens.to_dict()) == route_summary_line(d_code_ens)

    d_fanout = plan_chat_execution_mode(
        "Audit auth, cache, and scheduling across multiple modules in parallel for threat model and race condition gaps.",
        tier_models=_DUAL_PC_TIERS,
        developer_authorized=True,
        policy=auto_policy,
    )
    fanout_summary = route_summary_line(d_fanout)
    assert "| fanout:local[code+reasoning]" in fanout_summary

    d_degraded = plan_chat_execution_mode(
        "Think hard and analyze deadlock and race condition trade-offs in this distributed architecture.",
        tier_models={"code": "bonsai-27b-pq2", "general": "bonsai-27b-pq2", "reasoning": "qwen3-80b"},
        developer_authorized=True,
        tier_latency_ms={"code": 900.0, "general": 950.0, "reasoning": 25000.0},
        policy=auto_policy,
    )
    deg_summary = route_summary_line(d_degraded, include_reason=True)
    assert "degraded:reasoning" in deg_summary
    assert "fallback:general,reasoning" in deg_summary
    assert "25000ms" in deg_summary
    assert "\n" not in deg_summary

    # Manual override + multiline reason sanitization
    manual_decision = ExecutionDecision(
        mode="single",
        band="hard",
        difficulty_score=0.75,
        recommended_tiers=("code",),
        ensemble_mode="prose",
        fanout_scope="local",
        reason="explicit\nsingle   override",
        signals=("explicit_hard_cue",),
        auto_selected=False,
    )
    manual_summary = route_summary_line(manual_decision, include_reason=True)
    assert manual_summary == "manual | hard (0.75) | single[code] | explicit single override"

    # Malformed / partial serialized dict never raises and stays clean single-line ASCII
    malformed = {
        "mode": "single",
        "band": "standard",
        "difficulty_score": float("nan"),
        "recommended_tiers": [],
        "auto_selected": False,
        "reason": "line1\r\nline2",
    }
    malformed_summary = route_summary_line(malformed, include_reason=True)
    assert malformed_summary == "manual | standard (0.00) | single | line1 line2"
    assert malformed_summary.isascii()


def test_multi_target_audit_distinguishes_targets_from_criteria():
    # Three distinct targets joined by 'and' before 'for' -> multi_scope
    band_targets, _, sig_targets, _, _ = score_difficulty(
        "Audit auth and cache and scheduler for threat model gaps and race conditions."
    )
    assert band_targets == "multi_scope"
    assert "multi_target_audit" in sig_targets

    # Two files reviewed for two criteria -> hard (NOT multi_target_audit)
    band_two_files, _, sig_two_files, _, _ = score_difficulty(
        "Review router.py and service.py for subtle bugs and regressions."
    )
    assert band_two_files == "hard"
    assert "multi_target_audit" not in sig_two_files


