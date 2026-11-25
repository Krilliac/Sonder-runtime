"""Account boundaries for the HTTP task/checklist surface."""
import json
import re

import server
import sonder_runtime.interfaces.http.serve as serve


def _account(name):
    # Developer role: these tests are about account scoping. The role tool gate
    # (tests/test_role_tool_gate.py) leaves a user-role account no task tools.
    return {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": name, "role": "developer"},
    }


def _task_id(text):
    match = re.search(r"\b([0-9a-f]{8})\b", text)
    assert match, text
    return match.group(1)


def test_served_todos_are_account_scoped_but_local_mcp_stays_global(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    alice = _account("alice")
    bob = _account("bob")
    created = serve._handle_slash("/todo add Alice private task", context=alice)
    task_id = _task_id(created)

    assert "Alice private task" in serve._handle_slash("/todo list", context=alice)
    assert "no matching tasks" in serve._handle_slash("/todo list", context=bob)
    assert "no task" in serve._handle_slash("/todo show %s" % task_id, context=bob)
    # Local-open preserves the legacy global operator view, including scoped
    # rows, while account-backed callers remain fail-closed to other scopes.
    assert "Alice private task" in serve._handle_slash("/todo list")

    assert "task created" in server.task_create("local global task")
    assert "local global task" in server.task_list()


def test_catalogued_task_and_checklist_tools_cannot_cross_account_or_leak_latest(every_tool_allowed_by_rule, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    alice = _account("alice")
    bob = _account("bob")
    task = serve._dispatch_catalogued_tool("/task_create title=private", serve._LEGACY_STATE, alice)
    task_id = _task_id(task)
    assert "private" in serve._dispatch_catalogued_tool("/task_list", serve._LEGACY_STATE, alice)
    assert "no matching tasks" in serve._dispatch_catalogued_tool("/task_list", serve._LEGACY_STATE, bob)
    assert "no unique task" in serve._dispatch_catalogued_tool(
        "/task_delete task_id=%s" % task_id, serve._LEGACY_STATE, bob,
    )

    scope = serve._task_account_scope(alice)
    checklist = server.scoped_task_tool_dispatch(
        "checklist_create", {"title": "Alice plan", "items_json": '["one"]'},
        account_scope=scope,
    )
    assert "Alice plan" in checklist
    assert "Alice plan" in serve._handle_slash("/checklist", context=alice)
    assert "no checklist yet" in serve._handle_slash("/checklist", context=bob)


def test_scoped_task_show_events_alias_and_latest_checklist_use_updated_order(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    alice = _account("alice")
    scope = serve._task_account_scope(alice)
    task_id = _task_id(server.scoped_task_tool_dispatch(
        "task_create", {"title": "private"}, account_scope=scope,
    ))
    assert "events:" not in server.scoped_task_tool_dispatch(
        "task_show", {"task_id": task_id, "events": False}, account_scope=scope,
    )

    older = server.scoped_task_tool_dispatch(
        "checklist_create", {"title": "older", "items_json": '["one"]', "priority": 1},
        account_scope=scope,
    )
    newer = server.scoped_task_tool_dispatch(
        "checklist_create", {"title": "newer", "items_json": '["two"]', "priority": 2},
        account_scope=scope,
    )
    assert "older" in older and "newer" in newer
    # Priority sorting puts "older" first in a normal task list; the bare
    # checklist command must instead choose its newest durable parent.
    assert "newer" in serve._handle_slash("/checklist", context=alice)


def test_task_ledger_is_public_and_account_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    public_plan = server.task_plan("public goal", '["research", "validate"]')
    public_id = re.search(r"checklist ([0-9a-f]{8})", public_plan).group(1)
    payload = json.loads(server.task_ledger(public_id))
    assert payload["ledger"]["schema"] == "sonder.task-ledger.v1"
    assert payload["digest"]
    assert len(payload["ledger"]["items"]) == 2

    alice = _account("alice")
    scope = serve._task_account_scope(alice)
    scoped_plan = server.scoped_task_tool_dispatch(
        "task_plan", {"title": "Alice goal", "steps": ["one", "two"]},
        account_scope=scope,
    )
    scoped_id = re.search(r"checklist ([0-9a-f]{8})", scoped_plan).group(1)
    scoped_payload = json.loads(server.scoped_task_tool_dispatch(
        "task_ledger", {"goal_id": scoped_id}, account_scope=scope,
    ))
    assert scoped_payload["ledger"]["goal_id"].startswith(scoped_id)
    cross_ledger = server.scoped_task_tool_dispatch(
        "task_ledger", {"goal_id": public_id}, account_scope=scope,
    )
    assert "public goal" not in cross_ledger
    assert public_id not in cross_ledger or "no " in cross_ledger.lower() or "error" in cross_ledger.lower()


def test_task_scope_is_opaque_and_only_accounts_receive_one():
    scope = serve._task_account_scope(_account("alice"))
    assert scope.startswith("ta-")
    assert "alice" not in scope
    assert scope == serve._task_account_scope(_account("alice"))
    assert scope != serve._task_account_scope(_account("bob"))
    assert serve._task_account_scope({}) is None


def test_account_http_rejects_indirect_global_task_paths(monkeypatch):
    alice = _account("alice")
    alice_scope = serve._task_account_scope(alice)
    calls = []
    observed_scopes = []

    def fake_workbench(**kwargs):
        observed_scopes.append(("workbench_agent", server.active_task_account_scope(), kwargs))
        calls.append(kwargs)
        return "ran"

    def fake_agent(**kwargs):
        observed_scopes.append(("agent", server.active_task_account_scope(), kwargs))
        calls.append(kwargs)
        return "agent ran"

    monkeypatch.setattr(server, "workbench_agent", fake_workbench)
    monkeypatch.setattr(server, "agent", fake_agent)
    monkeypatch.setattr(server, "loop", lambda **kwargs: calls.append(kwargs) or "loop ran")
    monkeypatch.setattr(server, "master_orchestrate", lambda **kwargs: calls.append(kwargs) or "master ran")
    monkeypatch.setattr(server, "master_retry", lambda **kwargs: calls.append(kwargs) or "retry ran")
    monkeypatch.setattr(
        server, "control_command",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "control ran",
    )

    # Scoped autonomous /work, /agent (including /agent ensemble), and catalogued
    # /workbench_agent + /agent now execute inside Alice's active account boundary.
    assert serve._handle_slash("/work inspect this", context=alice) == "ran"
    assert serve._handle_slash("/agent ensemble make a random python game", context=alice) == "ran"
    assert serve._dispatch_catalogued_tool(
        "/workbench_agent prompt=inspect", serve._LEGACY_STATE, alice,
    ) == "ran"
    assert serve._dispatch_catalogued_tool(
        "/agent prompt=inspect", serve._LEGACY_STATE, alice,
    ) == "agent ran"
    assert len(observed_scopes) == 4
    assert all(scope == alice_scope for _tool, scope, _kw in observed_scopes)
    assert server.active_task_account_scope() is None
    calls.clear()

    # Unscoped loop actions and master/retry/control paths stay fail-closed.
    assert "loop action 'checklist_create'" in serve._dispatch_catalogued_tool(
        '/loop actions_json=[{"type":"checklist_create","title":"x"}]',
        serve._LEGACY_STATE, alice,
    )
    assert not calls
    assert "loop action 'workbench_agent'" in serve._dispatch_catalogued_tool(
        '/loop actions_json=[{"type":"workbench_agent","prompt":"inspect"}]',
        serve._LEGACY_STATE, alice,
    )
    assert not calls
    assert "account-scoped task state" in serve._handle_slash(
        "/master inline inspect this", context=alice,
    )
    assert not calls
    assert "account-scoped task state" in serve._dispatch_catalogued_tool(
        "/master_retry agent_id=abc", serve._LEGACY_STATE, alice,
    )
    assert not calls
    assert "account-scoped task state" in serve._handle_slash(
        "/agentretry abc", context=alice,
    )
    assert not calls
    assert "loop action 'master_retry'" in serve._dispatch_catalogued_tool(
        '/loop actions_json=[{"type":"master_retry","agent_id":"abc"}]',
        serve._LEGACY_STATE, alice,
    )
    assert not calls
    assert "loop action 'agent_retry'" in serve._dispatch_catalogued_tool(
        '/loop actions_json=[{"action":"agent_retry","agent_id":"abc"}]',
        serve._LEGACY_STATE, alice,
    )
    assert not calls

    # Local-open/direct operator use remains backward-compatible and global.
    observed_scopes.clear()
    assert serve._handle_slash("/work inspect this") == "ran"
    assert observed_scopes[-1][1] is None
    assert serve._handle_slash("/master inline inspect this") == "master ran"
    assert serve._dispatch_catalogued_tool(
        "/master_retry agent_id=abc", serve._LEGACY_STATE,
    ) == "retry ran"
    assert serve._handle_slash("/agentretry abc") == "control ran"
    assert calls


def test_account_http_rejects_global_saved_workflow_library(every_tool_allowed_by_rule, monkeypatch):
    """A hosted account must not see or operate the operator's workflow file."""
    # Use a fully authorized administrator: a normal user/developer is already
    # stopped by at least one role gate, while this tier could otherwise read
    # another account's shared workflow payloads and execute its actions.
    alice = {
        "account": {"username": "alice", "role": "admin"},
        "authorized": True,
        "mode": "account",
    }
    calls = []
    invocations = {
        "workflow_list": {},
        "workflow_save": {
            "name": "alice-private",
            "actions_json": '[{"type":"run_code","code":"secret"}]',
            "description": "private action payload",
        },
        "workflow_delete": {"name": "operator-flow"},
        "workflow_run": {"name": "operator-flow"},
    }

    for tool_name, values in invocations.items():
        monkeypatch.setattr(
            serve.command_catalog,
            "parse_invocation",
            lambda _line, name=tool_name, kwargs=values: (name, dict(kwargs)),
        )
        monkeypatch.setattr(
            serve._legacy_runtime(), tool_name,
            lambda **kwargs: calls.append((tool_name, kwargs)) or "private payload",
        )
        result = serve._dispatch_catalogued_tool(
            "/%s" % tool_name, serve._LEGACY_STATE, alice,
        )
        assert "account-scoped saved workflows" in result
        assert "private" not in result

    assert calls == []

    # Direct MCP/local-open remains the single-user operator surface.
    monkeypatch.setattr(
        serve.command_catalog,
        "parse_invocation", lambda _line: ("workflow_list", {}),
    )
    monkeypatch.setattr(serve._legacy_runtime(), "workflow_list", lambda: "operator flow")
    assert serve._dispatch_catalogued_tool("/workflow_list", serve._LEGACY_STATE) == "operator flow"


def test_account_scoped_agent_isolates_tasks_and_checklists_alice_vs_bob(
    every_tool_allowed_by_rule, tmp_path, monkeypatch,
):
    """Real /agent ensemble run for Alice scopes checklists & tasks away from Bob and operator."""
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    alice = _account("alice")
    bob = _account("bob")

    # Seed operator-global task and Bob's private task before Alice runs /agent.
    op_task = server.task_create("operator private milestone")
    assert "task created" in op_task
    bob_created = serve._handle_slash("/todo add Bob private roadmap", context=bob)
    bob_id = _task_id(bob_created)

    seen_systems = []
    playbook_cloud_flags = []
    tool_observations = []

    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict=None, **_kw: ("local-test-model", False, False, "code"),
    )
    monkeypatch.setattr(
        server, "_read_system_context",
        lambda: (
            "OPERATOR_SECRET_PROFILE",
            "OPERATOR_SECRET_EMOTIONS",
            "OPERATOR_SECRET_GOAL",
            "OPERATOR_SECRET_PLAYBOOK",
        ),
    )
    real_augment = server.playbook_context.augment

    def spy_augment(transcript, prompt, *, cloud=False, **kw):
        playbook_cloud_flags.append(bool(cloud))
        return real_augment(transcript, prompt, cloud=cloud, **kw)

    monkeypatch.setattr(server.playbook_context, "augment", spy_augment)

    scripted_decisions = iter([
        {"tool": "task_create", "args": {"title": "Alice python game"}, "reason": "track game"},
        {"tool": "task_list", "args": {"include_done": True}, "reason": "check visible tasks"},
        {"tool": "task_delete", "args": {"task_id": bob_id}, "reason": "adversarial cross-account delete"},
        {"final": "Built Alice's python game inside her scoped task state."},
    ])

    def fake_make_tier_generate(_tier, _model, system, *_a, **_kw):
        seen_systems.append(system)

        def _gen(step_prompt, *_ga, **_gkw):
            if "OBSERVATION" in step_prompt:
                tool_observations.append(step_prompt)
            return json.dumps(next(scripted_decisions))

        return _gen

    monkeypatch.setattr(server, "_make_tier_generate", fake_make_tier_generate)

    reply = serve._handle_slash("/agent ensemble make a random python game", context=alice)
    assert "Built Alice's python game" in reply
    assert seen_systems
    for secret_marker in (
        "OPERATOR_SECRET_PROFILE",
        "OPERATOR_SECRET_EMOTIONS",
        "OPERATOR_SECRET_GOAL",
        "OPERATOR_SECRET_PLAYBOOK",
    ):
        assert all(secret_marker not in s for s in seen_systems), secret_marker
    assert "caller-judged:" not in reply
    assert playbook_cloud_flags and all(playbook_cloud_flags)

    # Alice sees both her auto-checklist and her agent-created task.
    alice_list = serve._handle_slash("/todo list", context=alice)
    assert "Alice python game" in alice_list
    assert "Bob private roadmap" not in alice_list
    assert "operator private milestone" not in alice_list
    assert "HOST STRATEGY MODE: ensemble" in serve._handle_slash("/checklist", context=alice)

    # Bob never sees Alice's checklist or tasks, and Alice's adversarial delete failed.
    bob_list = serve._handle_slash("/todo list", context=bob)
    assert "Bob private roadmap" in bob_list
    assert "Alice python game" not in bob_list
    assert "no checklist yet" in serve._handle_slash("/checklist", context=bob)

    # Inside the agent loop, Alice's task_list saw neither Bob's nor operator's tasks.
    combined_obs = "\n".join(tool_observations)
    assert "Alice python game" in combined_obs
    assert "Bob private roadmap" not in combined_obs
    assert "operator private milestone" not in combined_obs


def test_account_scoped_agent_blocks_global_memory_workflows_and_operator_tools(
    tmp_path, monkeypatch,
):
    """Account-scoped agent runs hide and refuse every global-memory/workflow/operator tool."""
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    # Drift guard: every HTTP global-memory & saved-workflow tool must be blocked in scoped agents.
    assert serve._ACCOUNT_GLOBAL_MEMORY_TOOLS <= server._ACCOUNT_SCOPED_AGENT_BLOCKED_TOOLS
    assert serve._ACCOUNT_UNSCOPED_SAVED_WORKFLOW_TOOLS <= server._ACCOUNT_SCOPED_AGENT_BLOCKED_TOOLS

    alice_scope = serve._task_account_scope(_account("alice"))
    assert "remembered" in server.sonder_remember_fact(
        "operator-only secret fact", project="secret-proj",
    ).lower()

    captured_offload = {}
    monkeypatch.setattr(
        server, "offload",
        lambda **kwargs: captured_offload.update(kwargs) or "stateless-ok",
    )

    with server.scoped_task_account_boundary(alice_scope):
        scoped_help = server._agent_tool_help()
        for hidden in (
            "memory_search", "apply_learned", "workflow_run", "master_orchestrate",
            "master_retry", "agent_lane", "self_heal_repair", "preferences_status",
            "learn_preference", "set_context_size", "context_health", "diagnostics",
        ):
            assert f"\n- {hidden}:" not in scoped_help
            assert server._agent_run_tool_refusal(hidden) in (
                "account-scoped agent boundary", "system operation",
            )

        with server._managed_agent_admission_scope():
            for blocked in (
                "memory_search", "recall", "playbook_read", "playbook_note",
                "apply_learned", "workflow_list", "workflow_run", "master_orchestrate",
                "master_retry", "agent_lane", "loop", "self_heal_repair",
                "preferences_status", "set_context_size",
            ):
                res = server._agent_dispatch(blocked, {})
                assert "ERROR: HOST POLICY" in res, (blocked, res)
                if blocked not in server._AGENT_SYSTEM_OPERATOR_TOOLS:
                    assert "account-scoped agent run" in res, (blocked, res)

            # Nested offload stays stateless (no global memory augmentation or interaction write).
            assert server._agent_dispatch(
                "offload", {"prompt": "hi", "tier": "code", "learn": True, "session": "op-sess"},
            ) == "stateless-ok"
            assert captured_offload["learn"] is False
            assert captured_offload["session"] == "none"

        # Ensemble fact injection skips global operator project facts while account-scoped.
        assert server._ensemble_prompt_with_project_facts("build game", "secret-proj") == "build game"

    # Outside the boundary, operator global facts and tools work normally again.
    assert "operator-only secret fact" in server._ensemble_prompt_with_project_facts(
        "build game", "secret-proj",
    )


def test_account_scoped_boundary_non_widening_and_thread_propagation(tmp_path, monkeypatch):
    """Active account scope cannot be widened/switched and propagates via copy_context()."""
    import contextvars
    import threading
    import pytest
    from sonder_runtime.adapters.task_repository import TaskRepositoryAdapter

    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    alice_scope = serve._task_account_scope(_account("alice"))
    bob_scope = serve._task_account_scope(_account("bob"))

    with server.scoped_task_account_boundary(alice_scope):
        with pytest.raises(PermissionError):
            with server.scoped_task_account_boundary(None):
                pass
        with pytest.raises(PermissionError):
            with server.scoped_task_account_boundary(bob_scope):
                pass
        # Idempotent re-entry with identical scope succeeds.
        with server.scoped_task_account_boundary(alice_scope) as inner:
            assert inner == alice_scope

        # Repository default scope refuses explicit cross-account override.
        conn = server._open_db()
        try:
            repo = TaskRepositoryAdapter(conn, default_account_scope=alice_scope)
            with pytest.raises(PermissionError):
                repo.list(account_scope=bob_scope)
        finally:
            conn.close()

        # Propagates cleanly into child threads started with copy_context() (delegation/WorkRunner).
        child_out = {}

        def _child():
            child_out["scope"] = server.active_task_account_scope()
            child_out["created"] = server.task_create("child thread scoped task")
            with server._managed_agent_admission_scope():
                child_out["blocked"] = server._agent_dispatch("memory_search", {"query": "secret"})

        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(_child,))
        t.start()
        t.join(timeout=5)
        assert child_out["scope"] == alice_scope
        assert "task created" in child_out["created"]
        assert "account-scoped agent run" in child_out["blocked"]

    # Child thread's created task belongs only to Alice, never Bob.
    assert "child thread scoped task" in serve._handle_slash("/todo list", context=_account("alice"))
    assert "no matching tasks" in serve._handle_slash("/todo list", context=_account("bob"))


def test_catalogued_agent_namespaces_session_and_rejects_outside_project(monkeypatch):
    """Catalogued /agent & /workbench_agent namespace session and enforce project/cwd confinement."""
    alice = _account("alice")
    captured = []
    monkeypatch.setattr(
        server, "agent",
        lambda **kwargs: captured.append(dict(kwargs)) or "ok",
    )
    monkeypatch.setattr(
        server, "workbench_agent",
        lambda **kwargs: captured.append(dict(kwargs)) or "ok",
    )
    monkeypatch.setattr(
        server, "served_project_refusal",
        lambda path: "Refused: outside allowed workspace roots." if path and "outside" in str(path) else "",
    )

    assert "outside allowed workspace roots" in serve._dispatch_catalogued_tool(
        "/agent prompt=hi project=/outside/secret", serve._LEGACY_STATE, alice,
    )
    assert "outside allowed workspace roots" in serve._dispatch_catalogued_tool(
        "/agent prompt=hi cwd=/outside/secret", serve._LEGACY_STATE, alice,
    )
    assert "outside allowed workspace roots" in serve._dispatch_catalogued_tool(
        "/workbench_agent prompt=hi project=/outside/secret", serve._LEGACY_STATE, alice,
    )
    assert captured == []

    # Session names are principal-namespaced; explicit "none" stays opt-out.
    assert serve._dispatch_catalogued_tool(
        "/agent prompt=hi session=bob-private", serve._LEGACY_STATE, alice,
    ) == "ok"
    assert captured[-1]["session"].startswith("http-session-")
    assert "bob-private" not in captured[-1]["session"]

    assert serve._dispatch_catalogued_tool(
        "/workbench_agent prompt=hi session=none", serve._LEGACY_STATE, alice,
    ) == "ok"
    assert captured[-1]["session"] == "none"

    # Empty or whitespace session must normalize to "none", never global "default".
    assert serve._dispatch_catalogued_tool(
        '/agent prompt=hi session=""', serve._LEGACY_STATE, alice,
    ) == "ok"
    assert captured[-1]["session"] == "none"

    assert serve._dispatch_catalogued_tool(
        '/workbench_agent prompt=hi session="   "', serve._LEGACY_STATE, alice,
    ) == "ok"
    assert captured[-1]["session"] == "none"


def test_scoped_boundary_rejects_empty_and_whitespace_scope():
    """Empty/whitespace scopes fail closed; autopilot boundary isolates only ta-... owners."""
    import pytest
    from sonder_runtime.application.tasks.account_scope import autopilot_account_boundary

    for bad in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="must not be empty"):
            with server.scoped_task_account_boundary(bad):
                pass

    alice_scope = serve._task_account_scope(_account("alice"))
    bob_scope = serve._task_account_scope(_account("bob"))

    # Local REPL console owner ("rc-...") is not a hosted account scope.
    with autopilot_account_boundary("rc-deadbeef") as active:
        assert active is None
        assert server.active_task_account_scope() is None

    # Hosted account owner ("ta-...") enters the boundary and refuses switching.
    with autopilot_account_boundary(alice_scope) as active:
        assert active == alice_scope
        assert server.active_task_account_scope() == alice_scope
        with pytest.raises(PermissionError):
            with autopilot_account_boundary(bob_scope):
                pass


def test_speculation_engine_preserves_scope_and_blocks_context_health(tmp_path, monkeypatch):
    """SpeculationEngine preserves account scope across worker threads and never leaks context_health."""
    import sonder_speculation

    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    alice_scope = serve._task_account_scope(_account("alice"))

    # 1. Real SpeculationEngine worker thread inherits active_task_account_scope().
    observed_in_worker = []
    predictor = sonder_speculation.BranchPredictor(path=tmp_path / "predictor.json")

    def _worker_dispatch(tool_name, args):
        observed_in_worker.append((tool_name, server.active_task_account_scope()))
        return "ok", True

    engine = sonder_speculation.SpeculationEngine(predictor, _worker_dispatch, enabled=True)
    with server.scoped_task_account_boundary(alice_scope):
        assert engine.begin("status", "status:{}", {})
        res = engine.resolve("status:{}", "status")
        assert res is not None and res.ok
    assert observed_in_worker == [("status", alice_scope)]

    # 2. Inside _agent_turn, a predictor guessing context_health must neither begin nor retire it.
    context_health_called = []
    monkeypatch.setattr(
        server, "context_health",
        lambda *_a, **_kw: context_health_called.append(True) or "SECRET_OPERATOR_CONTEXT",
    )
    monkeypatch.setattr(server.sonder_speculation, "speculation_enabled", lambda: True)

    class _AlwaysContextHealthPredictor(sonder_speculation.BranchPredictor):
        def predict_next_tool(self, state):
            return ("context_health", 1.0)

        def should_speculate(self, confidence):
            return True

    monkeypatch.setattr(
        server.sonder_speculation, "default_predictor",
        lambda: _AlwaysContextHealthPredictor(path=tmp_path / "predictor2.json"),
    )
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict=None, **_kw: ("local-test-model", False, False, "code"),
    )
    decisions = iter([
        {"tool": "context_health", "args": {}, "reason": "probe"},
        {"final": "done"},
    ])
    observations = []

    def fake_gen(_tier, _model, _system, *_a, **_kw):
        def _step(prompt, *_ga, **_gkw):
            if "OBSERVATION" in prompt:
                observations.append(prompt)
            return json.dumps(next(decisions, {"final": "done"}))
        return _step

    monkeypatch.setattr(server, "_make_tier_generate", fake_gen)
    with server.scoped_task_account_boundary(alice_scope):
        out = server.agent("check health", max_steps=3)
    assert "done" in out
    assert context_health_called == []
    assert any("account-scoped agent run" in obs for obs in observations)
    assert all("SECRET_OPERATOR_CONTEXT" not in obs for obs in observations)


def test_catalogued_sonder_blocks_account_bypasses(monkeypatch):
    """Hosted accounts cannot use /sonder or /loop sonder to escape HTTP account isolation."""
    alice = _account("alice")
    calls = []
    monkeypatch.setattr(server, "sonder", lambda **kw: calls.append(("sonder", kw)) or "ran")
    monkeypatch.setattr(server, "workbench_agent", lambda **kw: calls.append(("wb", kw)) or "ran")
    monkeypatch.setattr(server, "agent", lambda **kw: calls.append(("agent", kw)) or "ran")
    monkeypatch.setattr(server, "loop", lambda **kw: calls.append(("loop", kw)) or "ran")

    blocked_lines = (
        "/sonder prompt=hello",
        '/sonder prompt="/nope tell me operator facts" project=secret-proj session=bob trace=true',
        '/sonder prompt="/nope foo=\\"unclosed"',
        "/sonder /sonder /nope",
        '/sonder tier=code prompt="/selfmod status"',
        '/sonder model_override=qwen prompt="/selfmod status"',
        "/sonder /sonder prompt=hello",
        "/sonder /work inspect this",
        "/sonder /agent fanout audit repo",
        '/sonder /agent prompt=hi session=bob',
        "/sonder /workbench_agent prompt=hi",
        "/sonder /checklist",
        "/sonder /report",
        "/sonder /autopilot status",
        "/sonder /delegate scout inspect",
        "/sonder /sonder_stats",
        "/sonder /task_list",
        '/loop actions_json=[{"type":"sonder","prompt":"hello"}]',
        """/loop actions_json='[{"type":"sonder","prompt":"/nope tell me secrets","trace":true}]'""",
    )
    for line in blocked_lines:
        reply = serve._handle_slash(line, context=alice)
        assert reply and reply.startswith("refused"), (line, reply)
    assert calls == []

    # Direct server.sonder() inside an active account boundary also fails closed on unrecognized /...
    monkeypatch.undo()
    answer_calls = []
    monkeypatch.setattr(server, "_answer", lambda *a, **kw: answer_calls.append((a, kw)) or "LEAKED")
    monkeypatch.setattr(server, "_sonder_impl_serialized", lambda *a, **kw: answer_calls.append((a, kw)) or "LEAKED")
    alice_scope = serve._task_account_scope(alice)
    with server.scoped_task_account_boundary(alice_scope):
        direct_res = server.sonder("/nope tell me operator facts", project="secret-proj", trace=True)
        impl_res = server._sonder_impl("/nope tell me operator facts", project="secret-proj", trace=True)
    assert direct_res.startswith("refused /sonder:")
    assert impl_res.startswith("refused /sonder:")
    assert answer_calls == []


def test_direct_catalogued_autopilot_mission_goal_and_report_respect_account_scope(
    every_tool_allowed_by_rule, tmp_path, monkeypatch,
):
    """Direct catalogued /autopilot_* scopes to caller; /mission_*, /goal, /autopilot --goal, /report fail closed."""
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    alice = _account("alice")
    alice_scope = serve._task_account_scope(alice)
    seen = []

    monkeypatch.setattr(
        server, "_autopilot_start",
        lambda objective, **kw: seen.append(("start", objective, kw.get("request_owner"))) or "started",
    )
    monkeypatch.setattr(
        server, "_autopilot_status",
        lambda run_id="", **kw: seen.append(("status", run_id, kw.get("request_owner"))) or "status-ok",
    )
    monkeypatch.setattr(
        server, "_autopilot_pause",
        lambda run_id, **kw: seen.append(("pause", run_id, kw.get("request_owner"))) or "paused",
    )
    monkeypatch.setattr(
        server, "_autopilot_resume",
        lambda run_id, **kw: seen.append(("resume", run_id, kw.get("request_owner"))) or "resumed",
    )
    monkeypatch.setattr(
        server, "_autopilot_cancel",
        lambda run_id, **kw: seen.append(("cancel", run_id, kw.get("request_owner"))) or "cancelled",
    )

    assert serve._dispatch_catalogued_tool('/autopilot_start objective="build app"', serve._LEGACY_STATE, alice) == "started"
    assert serve._dispatch_catalogued_tool("/autopilot_status", serve._LEGACY_STATE, alice) == "status-ok"
    assert serve._dispatch_catalogued_tool("/autopilot_pause run_id=bob-run", serve._LEGACY_STATE, alice) == "paused"
    assert serve._dispatch_catalogued_tool("/autopilot_resume run_id=bob-run", serve._LEGACY_STATE, alice) == "resumed"
    assert serve._dispatch_catalogued_tool("/autopilot_cancel run_id=bob-run", serve._LEGACY_STATE, alice) == "cancelled"
    assert [owner for _op, _arg, owner in seen] == [alice_scope] * 5

    # Unscoped goal/mission/composition and global activity /report are refused for hosted accounts.
    goal_called = []
    monkeypatch.setattr(
        server, "_goal_command",
        lambda *a, **kw: goal_called.append((a, kw)) or "GOAL_LEAKED",
    )
    monkeypatch.setattr(
        server, "mission_start",
        lambda *a, **kw: goal_called.append((a, kw)) or "MISSION_LEAKED",
    )
    monkeypatch.setattr(
        server, "mission_status",
        lambda *a, **kw: goal_called.append((a, kw)) or "MISSION_LEAKED",
    )
    monkeypatch.setattr(
        server, "composition_bindings",
        lambda *a, **kw: goal_called.append((a, kw)) or "COMP_LEAKED",
    )
    import goal_store as _gs
    monkeypatch.setattr(
        _gs, "get_active",
        lambda: goal_called.append(("active_goal",)) or {"id": "g-op", "objective": "OPERATOR_SECRET_GOAL"},
    )

    for line in (
        '/goal set "escalate" --auto',
        "/goals",
        "/autopilot run --goal",
        "/autopilot ensemble --goal",
        "/report",
        "/endreport",
    ):
        reply = serve._handle_slash(line, context=alice)
        assert reply and reply.startswith("refused"), (line, reply)
        assert "OPERATOR_SECRET_GOAL" not in reply

    for tool_line in (
        '/mission_start objective="takeover" auto=true',
        "/mission_status",
        "/composition_bindings",
    ):
        reply = serve._dispatch_catalogued_tool(tool_line, serve._LEGACY_STATE, alice)
        assert reply and reply.startswith("refused"), (tool_line, reply)

    assert goal_called == []


def test_route_work_request_and_autopilot_enforce_account_isolation(tmp_path, monkeypatch):
    """Account-scoped route_work_request refuses fleet, scopes autopilot snapshot/start, and propagates scope."""
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.intents, "containment_egress_refusal", lambda _t: None)
    monkeypatch.setattr(server.master_orchestrator, "requested_worker_cap", lambda _t: None)
    monkeypatch.setattr(server.runtime_policy, "route_tier", lambda *_a, **_kw: "code")
    monkeypatch.setattr(server, "_capability_refined_tier", lambda _p, tier, reason: (tier, reason))

    alice_scope = serve._task_account_scope(_account("alice"))
    bob_scope = serve._task_account_scope(_account("bob"))

    # 1. Fleet mode under account scope is refused without calling master_orchestrate.
    master_calls = []
    monkeypatch.setattr(server, "master_orchestrate", lambda **kw: master_calls.append(kw) or "fleet")
    monkeypatch.setattr(
        server.intents, "classify_execution",
        lambda _t: {"mode": "fleet", "reason": "multi-agent", "plan_only": False},
    )
    with server.scoped_task_account_boundary(alice_scope):
        fleet_reply = server._route_work_request("audit the codebase in parallel")
    assert "account-scoped task state" in fleet_reply
    assert master_calls == []

    # 2. Autopilot mode scopes snapshot and _autopilot_start to Alice's owner key.
    snapshot_owners = []
    started_calls = []

    def fake_snapshot(*, include_finished=False, limit=20, request_owner=None):
        snapshot_owners.append(request_owner)
        if request_owner == bob_scope:
            return {"runs": [{"id": "bob-run", "status": "running", "objective": "Bob secret"}]}
        return {"runs": []}

    monkeypatch.setattr(server.autopilot_store, "snapshot", fake_snapshot)
    monkeypatch.setattr(
        server, "_autopilot_start",
        lambda *a, **kw: started_calls.append((a, kw)) or "autopilot started",
    )
    monkeypatch.setattr(
        server.intents, "classify_execution",
        lambda _t: {"mode": "autopilot", "reason": "multi-step", "plan_only": False},
    )
    with server.scoped_task_account_boundary(alice_scope):
        ap_reply = server._route_work_request("build a full game end to end")
    assert "Bob secret" not in ap_reply
    assert "autopilot started" in ap_reply
    assert snapshot_owners == [alice_scope]
    assert started_calls and started_calls[0][1]["request_owner"] == alice_scope

    # 3. _launch_autopilot propagates scope and autopilot_account_boundary pins _autopilot_thread_main.
    thread_observed = []
    done_ev = __import__("threading").Event()

    def fake_execute(run_id, *, max_cycles=12, plan_only=False, request_owner=None):
        thread_observed.append((run_id, request_owner, server.active_task_account_scope()))
        done_ev.set()
        return {"status": "completed", "summary": "ok"}

    monkeypatch.setattr(server, "_execute_autopilot", fake_execute)
    monkeypatch.setattr(
        server, "_application",
        lambda: type("_App", (), {
            "automation": type("_Auto", (), {"get_run": staticmethod(lambda *_a, **_kw: {})})(),
        })(),
    )
    assert server._launch_autopilot("ap-test-1", max_cycles=1, request_owner=alice_scope)
    assert done_ev.wait(timeout=5)
    assert thread_observed == [("ap-test-1", alice_scope, alice_scope)]

    # 4. _autopilot_allowed_tools strips global-memory & blocked tools under account scope.
    with server.scoped_task_account_boundary(alice_scope):
        allowed = server._autopilot_allowed_tools({"policy": "workspace", "project": ""})
        assert allowed is not None
        for blocked in (
            "memory_search", "context_health", "diagnostics", "learning_health_status",
            "memory_quality_report", "system_improvement_report", "learn_tool",
            "game_generate_and_test",
        ):
            assert blocked not in allowed, blocked

    # 5. _build_system omits operator profile/emotions/goal/playbook while account-scoped.
    monkeypatch.setattr(
        server, "_read_system_context",
        lambda: ("OP_PROF", "OP_EMO", "OP_GOAL", "OP_PLAYBOOK"),
    )
    with server.scoped_task_account_boundary(alice_scope):
        sys_text = server._build_system("req-system", False, "", model="local-m", cloud=False)
    assert "req-system" in sys_text
    for leaked in ("OP_PROF", "OP_EMO", "OP_GOAL", "OP_PLAYBOOK"):
        assert leaked not in sys_text


def test_account_scoped_blocks_generic_fallback_direct_tools_and_standing_leaks(
    every_tool_allowed_by_rule, tmp_path, monkeypatch,
):
    """Generic agent dispatch, global tools, standing DB reads, and direct /offload + /ensemble stay isolated."""
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    alice = _account("alice")
    alice_scope = serve._task_account_scope(alice)
    assert "remembered" in server.sonder_remember_fact(
        "operator-only project secret", project="secret-proj",
    ).lower()

    # 1. Direct catalogued global-memory/learning/game tools are refused for hosted accounts.
    for cmd in (
        "/learn_tool tool=python",
        '/extract_grounded source=hello schema="{}"',
        "/sonder_authoritative_indexes",
        "/calibration_status",
        "/game_generate_and_test name=arena concept=rpg",
        "/game_generation_campaign name=fleet",
    ):
        res = serve._dispatch_catalogued_tool(cmd, serve._LEGACY_STATE, alice)
        assert "refused" in res.lower(), (cmd, res)

    # 2. Direct catalogued /ensemble_answer and /offload run inside Alice's boundary.
    seen_ensemble_scopes = []
    monkeypatch.setattr(
        server, "ensemble_answer",
        lambda **kw: seen_ensemble_scopes.append((server.active_task_account_scope(), kw)) or "ens-ok",
    )
    assert serve._dispatch_catalogued_tool(
        "/ensemble_answer prompt=hi project=secret-proj", serve._LEGACY_STATE, alice,
    ) == "ens-ok"
    assert seen_ensemble_scopes[-1][0] == alice_scope

    captured_tier_offload = []
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: captured_tier_offload.append(("should_learn", learn)) or False)
    monkeypatch.setattr(
        server, "_chat_request",
        lambda *_a, **_kw: ({"model": "local-code", "message": {"content": "stateless-reply"}}, "stateless-reply"),
    )
    with server.scoped_task_account_boundary(alice_scope):
        out = server._offload_tier_impl("hello", tier="code", learn=True, session="op-sess")
    assert "stateless-reply" in out
    assert ("should_learn", False) in captured_tier_offload

    # Empty-string session inside _agent_turn never resolves to global "default" under account scope.
    resolved_sessions = []
    orig_resolve_session = server._resolve_session
    monkeypatch.setattr(
        server, "_resolve_session",
        lambda s: resolved_sessions.append(s) or orig_resolve_session(s),
    )
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict=None, **_kw: ("local-test-model", False, False, "code"),
    )
    monkeypatch.setattr(
        server, "_make_tier_generate",
        lambda *_a, **_kw: (lambda *_ga, **_gkw: json.dumps({"final": "ok"})),
    )
    with server.scoped_task_account_boundary(alice_scope):
        assert "ok" in server.agent("quick check", max_steps=1, session="")
        assert "ok" in server.agent("quick check", max_steps=1, session="   ")
    assert resolved_sessions == []

    # 3. Generic fallback & newly blocked tools are hidden and refused inside account scope.
    with server.scoped_task_account_boundary(alice_scope):
        scoped_help = server._agent_tool_help()
        for blocked in (
            "learn_tool", "sonder_authoritative_indexes", "extract_grounded",
            "calibration_status", "game_generate_and_test", "game_generation_campaign",
            "autopilot_status", "turn_inspect", "reasoning_show", "batch_job_recent",
            "model_fanout_recent",
        ):
            assert f"\n- {blocked}:" not in scoped_help, blocked
        with server._managed_agent_admission_scope():
            for blocked in (
                "learn_tool", "sonder_authoritative_indexes", "extract_grounded",
                "calibration_status", "game_generate_and_test", "game_generation_campaign",
                "autopilot_status", "turn_inspect", "reasoning_show",
                "local_service_probe",
            ):
                res = server._agent_dispatch(blocked, {})
                assert "ERROR: HOST POLICY" in res, (blocked, res)
                assert "account-scoped agent run" in res, (blocked, res)

        # Standing helpers never read global operator outcomes DB while account-scoped.
        monkeypatch.setattr(
            server, "_open_db_readonly",
            lambda: (_ for _ in ()).throw(AssertionError("must not read global outcomes DB")),
        )
        demanded, reason = server._agent_verification_standing()
        assert demanded is True and "isolated" in reason
        assert "account-scoped" in server._agent_verification_standing_notice()
        assert server._agent_end_report_standing_line() == ""


def test_http_chat_tool_turn_scopes_account_tools_and_blocks_global_memory(
    every_tool_allowed_by_rule, tmp_path, monkeypatch,
):
    """HTTP ChatToolTurn under a hosted developer account strips global memory tools and pins task scope."""
    from sonder_runtime.bootstrap import chat_tools_wiring

    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(chat_tools_wiring, "_model_supports_tools", lambda _s, _m: True)

    alice = _account("alice")
    bob = _account("bob")
    alice_scope = serve._task_account_scope(alice)

    turn = chat_tools_wiring.build_http_chat_tool_turn(
        server=server,
        permission_policy=serve.permission_policy,
        handler=None,
        chunk_fn=lambda *_a, **_kw: "",
        model="sonder",
        project="",
        stream=False,
        authorized_tools=True,
        account_scope=alice_scope,
    )
    assert turn is not None
    admitted_names = {t.name for t in turn.manifest.tools}
    payload_names = {item["function"]["name"] for item in turn.tools_payload}
    for blocked in ("memory_search", "learn_tool"):
        assert blocked not in admitted_names
        assert blocked not in payload_names
        assert blocked in turn.blocked_tools

    # Tool search hides blocked global tools.
    search_res = turn.backend.execute("tool_search", {"name": "memory_search"})
    assert not search_res.ok

    # Backend dispatch runs inside Alice's account boundary and refuses blocked tools.
    with server._managed_agent_admission_scope():
        denied = turn.backend.execute("memory_search", {"query": "secret"})
        assert not denied.ok
        assert "account-scoped agent run" in denied.output

        created = turn.backend.execute("task_create", {"title": "Alice chat tool task"})
        assert created.ok
    assert "Alice chat tool task" in serve._handle_slash("/todo list", context=alice)
    assert "no matching tasks" in serve._handle_slash("/todo list", context=bob)
