"""Unit and HTTP facade tests for the difficulty-to-execution-mode router."""
from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace
from typing import Any

from sonder_runtime.application.routing.execution_mode_service import (
    ENV_AUTO_ENSEMBLE_TIERS,
    ENV_AUTO_EXECUTION_MODE,
    plan_chat_execution_mode,
    policy_from_env,
)
from sonder_runtime.domain.routing.execution_mode_router import (
    ExecutionContext,
    ExecutionModePolicy,
    check_execution_mode,
    plan_execution_mode,
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
