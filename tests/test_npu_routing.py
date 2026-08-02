"""Server integration for ambiguous execution routing: deterministic cues
stay authoritative, prefer falls back to the local router, shadow never
changes behavior, and accelerator errors can never break routing."""
import server


AMBIGUOUS = (
    "Inspect the repository, diagnose the API, and then fix the app before "
    "you run and validate all tests."
)


def _no_active(monkeypatch):
    monkeypatch.setattr(
        server.autopilot_store,
        "snapshot",
        lambda **_kwargs: {"active_runs": 0, "resumable_runs": 0, "runs": []},
    )


def _never_called(name):
    def _boom(*_args, **_kwargs):
        raise AssertionError("%s must not be called" % name)
    return _boom


def test_explicit_cue_bypasses_npu_entirely(monkeypatch):
    _no_active(monkeypatch)
    monkeypatch.setattr(
        server.npu_service, "route_decide", _never_called("route_decide"),
    )
    monkeypatch.setattr(
        server.npu_service, "route_shadow", _never_called("route_shadow"),
    )
    monkeypatch.setattr(
        server, "autopilot_start", lambda **kwargs: "autopilot started",
    )

    output = server.route_work_request(
        "Inspect the repo and keep working autonomously until all tests pass.",
    )

    assert "source: explicit host cue" in output


def test_prefer_decision_routes_without_local_model(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.npu_service,
        "route_decide",
        lambda prompt: {
            "mode": "workbench",
            "tier": "general",
            "reason": "npu accelerator (cpu-sim): score margin 0.74",
            "confidence": 0.87,
            "source": "npu accelerator",
        },
    )
    monkeypatch.setattr(
        server, "_execution_route_model",
        _never_called("_execution_route_model"),
    )
    monkeypatch.setattr(
        server, "workbench_agent",
        lambda **kwargs: calls.append(kwargs) or "work complete",
    )

    output = server.route_work_request(AMBIGUOUS)

    assert "source: npu accelerator" in output
    assert "confidence: 87%" in output
    assert "score margin" in output
    assert calls[0]["tier"] == "general"


def test_prefer_miss_falls_back_to_local_ollama_router(monkeypatch):
    calls = []
    monkeypatch.setattr(server.npu_service, "route_decide", lambda prompt: None)
    monkeypatch.setattr(
        server,
        "_execution_route_model",
        lambda *_args, **_kwargs: {
            "mode": "workbench",
            "tier": "code",
            "reason": "the stages fit one guarded loop",
            "confidence": 0.8,
        },
    )
    monkeypatch.setattr(
        server, "workbench_agent",
        lambda **kwargs: calls.append(kwargs) or "work complete",
    )

    output = server.route_work_request(AMBIGUOUS)

    assert "source: bounded local mode model" in output
    assert calls


def test_shadow_observes_baseline_without_changing_it(monkeypatch):
    shadows = []
    monkeypatch.setattr(server.npu_service, "route_decide", lambda prompt: None)
    monkeypatch.setattr(
        server.npu_service,
        "route_shadow",
        lambda prompt, baseline: shadows.append((prompt, baseline)),
    )
    monkeypatch.setattr(
        server,
        "_execution_route_model",
        lambda *_args, **_kwargs: {
            "mode": "workbench",
            "tier": "code",
            "reason": "fits one loop",
            "confidence": 0.7,
        },
    )
    monkeypatch.setattr(
        server, "workbench_agent", lambda **kwargs: "work complete",
    )

    output = server.route_work_request(AMBIGUOUS)

    assert "source: bounded local mode model" in output
    assert shadows == [(AMBIGUOUS, {"mode": "workbench", "tier": "code"})]


def test_accelerator_crash_never_breaks_routing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.npu_service,
        "route_decide",
        lambda prompt: (_ for _ in ()).throw(
            RuntimeError("accelerator exploded"),
        ),
    )
    monkeypatch.setattr(
        server.npu_service,
        "route_shadow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("shadow exploded"),
        ),
    )
    monkeypatch.setattr(
        server,
        "_execution_route_model",
        lambda *_args, **_kwargs: {
            "mode": "workbench",
            "tier": "code",
            "reason": "fits one loop",
            "confidence": 0.7,
        },
    )
    monkeypatch.setattr(
        server, "workbench_agent",
        lambda **kwargs: calls.append(kwargs) or "work complete",
    )

    output = server.route_work_request(AMBIGUOUS)

    assert "source: bounded local mode model" in output
    assert calls


def test_npu_failure_fallback_stays_local_never_cloud(monkeypatch):
    """The prefer-miss fallback chain is the local router, then Autopilot."""
    _no_active(monkeypatch)
    starts = []
    monkeypatch.setattr(server.npu_service, "route_decide", lambda prompt: None)
    monkeypatch.setattr(
        server,
        "_execution_route_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("local execution router model is unavailable"),
        ),
    )
    monkeypatch.setattr(
        server, "autopilot_start",
        lambda **kwargs: starts.append(kwargs) or "autopilot started",
    )

    output = server.route_work_request(AMBIGUOUS)

    assert "source: host fallback" in output
    assert "mode: persistent Autopilot" in output
    assert starts


def test_npu_status_tool_reports_accelerator(monkeypatch):
    monkeypatch.setattr(
        server.npu_service,
        "status",
        lambda probe=False: {"probe": probe},
    )
    monkeypatch.setattr(
        server.npu_service,
        "format_status",
        lambda state: "sonder npu accelerator\n  policy: routing=off",
    )

    output = server.npu_status()

    assert "sonder npu accelerator" in output


def test_diagnostics_includes_npu_line(monkeypatch):
    monkeypatch.setattr(
        server.npu_service, "diagnostics_line", lambda: "off (policy; not probed)",
    )
    output = server.diagnostics()
    assert "npu accelerator: off (policy; not probed)" in output


def test_live_reload_covers_npu_modules():
    for name in (
        "npu_contract", "npu_manifest", "npu_providers", "npu_broker",
        "npu_service",
    ):
        assert name in server.LIVE_RELOAD_MODULES
