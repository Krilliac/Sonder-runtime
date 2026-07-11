import server


def _no_active(monkeypatch):
    monkeypatch.setattr(
        server.autopilot_store,
        "snapshot",
        lambda **_kwargs: {"active_runs": 0, "resumable_runs": 0, "runs": []},
    )


def test_explicit_autonomy_starts_persistent_autopilot(monkeypatch):
    _no_active(monkeypatch)
    calls = []
    monkeypatch.setattr(
        server,
        "autopilot_start",
        lambda **kwargs: calls.append(kwargs) or "autopilot started",
    )

    output = server.route_work_request(
        "Inspect the repo and keep working autonomously until all tests pass.",
        project="demo",
    )

    assert "mode: persistent Autopilot" in output
    assert "source: explicit host cue" in output
    assert calls[0]["objective"].startswith("Inspect the repo")
    assert calls[0]["project"] == "demo"
    assert calls[0]["tier"] == "code"
    assert calls[0]["adaptive"] is True
    assert calls[0]["plan_only"] is False
    assert calls[0]["wait"] is False


def test_plan_only_natural_request_starts_pausing_plan(monkeypatch):
    _no_active(monkeypatch)
    calls = []
    monkeypatch.setattr(
        server,
        "autopilot_start",
        lambda **kwargs: calls.append(kwargs) or "plan started",
    )

    output = server.route_work_request(
        "Plan only: inspect the repo, fix the API, and validate the app tests."
    )

    assert "persistent Autopilot" in output
    assert calls[0]["plan_only"] is True


def test_explicit_fleet_uses_hardware_bounded_master(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "master_orchestrate",
        lambda **kwargs: calls.append(kwargs) or "fleet complete",
    )

    output = server.route_work_request(
        "Spawn as many parallel agents as the hardware allows to audit this repo."
    )

    assert "mode: hardware-bounded fleet" in output
    assert calls == [{
        "task": "Spawn as many parallel agents as the hardware allows to audit this repo.",
        "mode": "fleet",
        "tier": "code",
        "learn": False,
    }]


def test_simple_work_uses_foreground_without_model_triage(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "_execution_route_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("not needed")),
    )
    monkeypatch.setattr(
        server,
        "workbench_agent",
        lambda **kwargs: calls.append(kwargs) or "work complete",
    )

    output = server.route_work_request("Build the Flutter app.", project="demo")

    assert "mode: foreground workbench" in output
    assert "tier: code ->" in output
    assert calls[0]["project"] == "demo"
    assert calls[0]["allow_location"] is False


def test_compound_work_uses_bounded_local_model_decision(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "_execution_route_model",
        lambda *_args, **_kwargs: {
            "mode": "workbench",
            "tier": "general",
            "reason": "the stages fit one guarded loop",
            "confidence": 0.8,
        },
    )
    monkeypatch.setattr(
        server,
        "workbench_agent",
        lambda **kwargs: calls.append(kwargs) or "work complete",
    )

    output = server.route_work_request(
        "Inspect the repository, diagnose the API, and then fix the app before "
        "you run and validate all tests."
    )

    assert "source: bounded local mode model" in output
    assert "confidence: 80%" in output
    assert calls[0]["tier"] == "general"


def test_compound_route_falls_back_to_autopilot_safely(monkeypatch):
    _no_active(monkeypatch)
    calls = []
    monkeypatch.setattr(
        server,
        "_execution_route_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad JSON")),
    )
    monkeypatch.setattr(
        server,
        "autopilot_start",
        lambda **kwargs: calls.append(kwargs) or "autopilot started",
    )

    output = server.route_work_request(
        "Inspect the repository, diagnose the API, and then fix the app before "
        "you run and validate all tests."
    )

    assert "source: host fallback" in output
    assert "mode: persistent Autopilot" in output
    assert calls


def test_natural_autopilot_does_not_start_concurrent_run(monkeypatch):
    monkeypatch.setattr(
        server.autopilot_store,
        "snapshot",
        lambda **_kwargs: {
            "runs": [{
                "id": "auto-live",
                "status": "running",
                "objective": "existing goal",
            }],
        },
    )
    monkeypatch.setattr(
        server,
        "autopilot_start",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must defer")),
    )

    output = server.route_work_request(
        "Keep working autonomously to inspect and fix this repository."
    )

    assert "mode: Autopilot deferred" in output
    assert "auto-live [running] existing goal" in output


def test_local_route_model_repairs_schema_once(monkeypatch):
    responses = [
        "I would use autopilot.",
        '{"mode":"autopilot","tier":"code",'
        '"reason":"several dependent phases","confidence":0.9}',
    ]
    prompts = []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(
        server,
        "_serve_target",
        lambda *_args, **_kwargs: ("qwen-local", False, False, "fast"),
    )
    monkeypatch.setattr(server, "_make_generate", lambda *_args, **_kwargs: generate)

    result = server._execution_route_model("inspect, implement, and validate")

    assert result["mode"] == "autopilot"
    assert result["tier"] == "code"
    assert result["confidence"] == 0.9
    assert "HOST SCHEMA ERROR" in prompts[1]


def test_non_work_and_no_tools_requests_are_not_routed():
    assert server.route_work_request("How do I build a Flutter app?") is None
    assert server.route_work_request("Explain only how to fix this app") is None
