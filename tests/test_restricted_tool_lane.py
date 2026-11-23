"""A restricted model in the ``agent`` lane, end to end through ``server``, with the model faked.

The security-advisers floor profile is given read-only tools by an operator
overlay; the model is a scripted generator, the catalog a dictionary, and the
tools are the real dispatcher against a temporary project.  Every forged call
must be refused before dispatch, every other lane must keep refusing, and a
model with no tools granted must be refused exactly as before.
"""
from __future__ import annotations

import ast
import json
import re
import sys
import threading
from pathlib import Path

import pytest

import server
from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters import restricted_tool_lane as lane
from sonder_runtime.adapters import tool_lane_guard as guard
from tests.support.tool_lane import ALLOWED, BLOCKED, DENIED, DENIED_SMALL, RUNNABLE, clear_environment

REPO = Path(__file__).resolve().parent.parent
GRANTED = ["text_search", "file_read_range", "directory_tree", "context_pack", "repository_symbol_index"]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    (tmp_path / "overlay").mkdir()
    monkeypatch.setenv(mtp.OVERLAY_ENV, str(tmp_path / "overlay" / "model_tool_policy.json"))
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    monkeypatch.setattr(guard, "_GENERATION_CACHE", None, raising=False)
    monkeypatch.setattr(server, "_RUNTIME_POLICY", server._RUNTIME_POLICY)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *a, **k: None)
    server._refresh_runtime_policy(create=False)
    before = dict(server.TIERS)
    for tier in ("fast", "code", "general"):
        server.TIERS[tier] = ALLOWED
    mtp.clear_cache()
    yield
    mtp.clear_cache()
    server.TIERS.clear()
    server.TIERS.update(before)


@pytest.fixture
def catalog(monkeypatch):
    names = {DENIED: DENIED, DENIED_SMALL: DENIED_SMALL, ALLOWED: ALLOWED}

    def record(selector):
        key = str(selector or "").strip().casefold()
        return (names[key], {"name": names[key]}) if key in names else None

    monkeypatch.setattr(server, "resolve_discovered_model_record", record)
    monkeypatch.setattr(server, "_fanout_nonchat_reason", lambda record: "")
    return names


@pytest.fixture
def overlay(tmp_path):
    path = tmp_path / "overlay" / "model_tool_policy.json"

    def write(tools=GRANTED, lanes=("agent", "workbench_agent"), **extra):
        path.write_text(json.dumps({"version": 1, "model_profiles": [
            {"id": "security-advisers", "tools": list(tools), "lanes": list(lanes)},
        ], **extra}), encoding="utf-8")
        mtp.clear_cache()

    return write


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "src" / "app.py").write_text("def needle():\n    return 1\n", encoding="utf-8")
    (root / ".env").write_text("needle=TOPSECRET\n", encoding="utf-8")
    (root / "config" / "credentials.json").write_text('{"needle": "TOPSECRET"}\n', encoding="utf-8")
    (root / "server.pem").write_text("needle TOPSECRET\n", encoding="utf-8")
    return root


class Script:
    """A scripted model: returns each decision in turn and records every prompt."""

    def __init__(self, *decisions):
        self.decisions = [json.dumps(item) for item in decisions]
        self.prompts = []
        self.built = []

    def make(self, tier_label, model, *args, **kwargs):
        self.built.append((tier_label, model))

        def generate(prompt, *a, **k):
            self.prompts.append(prompt)
            return self.decisions.pop(0) if self.decisions else '{"final": "done"}'

        return generate


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    real = server._agent_dispatch_observed

    def record(tool_name, args, *a, **k):
        calls.append((tool_name, dict(args) if isinstance(args, dict) else args))
        return real(tool_name, args, *a, **k)

    monkeypatch.setattr(server, "_agent_dispatch_observed", record)
    return calls


def run(monkeypatch, script, **kwargs):
    monkeypatch.setattr(server, "_make_tier_generate", script.make)
    return server.agent("inspect the project", tier=DENIED, checklist=False, **kwargs)


def advertised(prompt):
    names = set(re.findall(r"^- ([a-z_]+):", prompt, flags=re.M))
    other = re.search(r"^other tools \(.*?\): (.*)$", prompt, flags=re.M)
    if other:
        names.update(item.strip() for item in other.group(1).split(","))
    return names


# ---- the restricted run ---------------------------------------------------------

def test_restricted_model_is_offered_only_granted_tools_and_searches_without_secrets(
    catalog, overlay, project, monkeypatch, dispatched,
):
    overlay()
    script = Script({"tool": "text_search", "args": {"query": "needle"}}, {"final": "found it"})
    output = run(monkeypatch, script, project=str(project))
    assert script.built == [("model:" + DENIED, DENIED)]  # the named model really answered
    offered = advertised(script.prompts[0])
    assert offered and offered <= set(GRANTED), offered
    assert dispatched and dispatched[0][0] == "text_search"
    observation = script.prompts[1]
    assert "app.py" in observation
    assert "TOPSECRET" not in observation and "TOPSECRET" not in output
    assert "found it" in output


@pytest.mark.parametrize("forged", [
    {"tool": "file_write", "args": {"path": "x.txt", "content": "x", "mode": "create"}},
    {"tool": "web_fetch", "args": {"url": "http://example.com"}},
    {"tool": "run_code", "args": {"code": "print(1)", "language": "python"}},
    {"tool": "memory_search", "args": {"query": "anything"}},
    {"tool": "file_read", "args": {"path": "src/app.py"}},
    {"tool": "tool_manifest", "args": {}},
])
def test_a_forged_call_to_an_ungranted_tool_is_refused_at_dispatch(
    catalog, overlay, project, monkeypatch, dispatched, forged,
):
    overlay()
    script = Script(forged, {"final": "done"})
    run(monkeypatch, script, project=str(project))
    assert dispatched == []
    assert "outside this autonomous run's allowlist" in script.prompts[1]
    assert not (project / "x.txt").exists()


@pytest.mark.parametrize("call", [
    {"tool": "file_read_range", "args": {"path": ".env"}},
    {"tool": "file_read_range", "args": {"path": "config/credentials.json"}},
    {"tool": "file_read_range", "args": {"path": "server.pem"}},
    {"tool": "context_pack", "args": {"paths_json": json.dumps(["src/app.py", ".env"])}},
    {"tool": "directory_tree", "args": {"path": ".ssh"}},
    {"tool": "text_search", "args": {"query": "needle", "glob": "*.pem"}},
    {"tool": "text_search", "args": {"query": "needle", "include_hidden": True}},
    {"tool": "file_read_range", "args": {"path": "../outside.txt"}},
])
def test_secret_or_outside_paths_are_refused(catalog, overlay, project, monkeypatch, dispatched, call):
    overlay()
    script = Script(call, {"final": "done"})
    output = run(monkeypatch, script, project=str(project))
    assert dispatched == []
    assert "HOST POLICY" in script.prompts[1]
    assert "TOPSECRET" not in script.prompts[1] and "TOPSECRET" not in output


def test_directory_tree_withholds_secret_names_and_does_not_descend_into_them(
    catalog, overlay, project, monkeypatch, dispatched,
):
    overlay()
    (project / "secrets").mkdir()
    (project / "secrets" / "plain_inside.txt").write_text("x\n", encoding="utf-8")
    (project / "src" / "deploy_token_notes.md").write_text("x\n", encoding="utf-8")
    script = Script({"tool": "directory_tree", "args": {"path": ".", "depth": 4}}, {"final": "done"})
    run(monkeypatch, script, project=str(project))
    assert [name for name, _args in dispatched] == ["directory_tree"]
    observation = script.prompts[1]
    assert "app.py" in observation
    for withheld in ("credentials.json", "server.pem", "secrets", "plain_inside.txt", "deploy_token_notes.md"):
        assert withheld not in observation, (withheld, observation)
    assert "withheld" in observation


def test_the_tree_filter_checks_every_entry_line_and_withholds_what_it_cannot_parse(project):
    run = _run()
    run.project_root = str(project)
    run.secret_globs = (".env", "*.pem", "**/secrets/**", "*secret*")
    observation = "\n".join([
        "directory tree: %s" % project,
        "  [D] config",
        "    [F] config/settings.toml (12 bytes)",
        "  [D] vault_secret_dir",
        "    [F] vault_secret_dir/readme.txt (3 bytes)",
        "  [F] server.pem (9 bytes)",
        "  [F] notes (1 bytes).txt (4 bytes)",
        "garbage line that is not an entry",
        "  ... truncated at 5 entries",
    ])
    filtered = run.filter_observation("directory_tree", {"path": str(project)}, observation).split("\n")
    assert filtered[0] == "directory tree: %s" % project
    assert "  [D] config" in filtered and "    [F] config/settings.toml (12 bytes)" in filtered
    assert "  [F] notes (1 bytes).txt (4 bytes)" in filtered
    assert "  ... truncated at 5 entries" in filtered
    text = "\n".join(filtered)
    for withheld in ("vault_secret_dir", "readme.txt", "server.pem", "garbage"):
        assert withheld not in text, withheld


def test_no_project_is_refused(catalog, overlay, monkeypatch):
    overlay()
    script = Script({"final": "done"})
    output = run(monkeypatch, script)
    assert "a restricted model needs a project root" in output
    assert script.prompts == []


def test_a_project_label_is_not_a_project_root(catalog, overlay, monkeypatch):
    overlay()
    script = Script({"final": "done"})
    output = run(monkeypatch, script, project="default")
    assert "a restricted model needs a project root" in output
    assert script.prompts == []


def test_a_lane_not_listed_in_the_profile_keeps_the_refusal(catalog, overlay, project, monkeypatch):
    overlay(lanes=["agent"])
    script = Script({"final": "done"})
    monkeypatch.setattr(server, "_make_tier_generate", script.make)
    output = server.workbench_agent("inspect", tier=DENIED, project=str(project))
    assert (BLOCKED % DENIED).lower() in output.lower()
    assert script.prompts == []


def test_explicit_tier_workbench_agent_is_a_restricted_lane(catalog, overlay, project, monkeypatch, dispatched):
    overlay(lanes=["workbench_agent"])
    script = Script({"tool": "directory_tree", "args": {}}, {"final": "listed"})
    monkeypatch.setattr(server, "_make_tier_generate", script.make)
    output = server.workbench_agent("inspect", tier=DENIED, project=str(project))
    assert "listed" in output
    assert [name for name, _args in dispatched] == ["directory_tree"]
    # ...but the agent tool itself is not, with that profile
    script = Script({"final": "done"})
    assert (BLOCKED % DENIED).lower() in run(monkeypatch, script, project=str(project)).lower()


@pytest.mark.parametrize("grant", [None, {"tools": [], "lanes": ["agent", "workbench_agent"]}, {"tools": [], "lanes": []}])
def test_with_no_tools_granted_the_refusal_is_identical_to_today(catalog, overlay, project, monkeypatch, grant):
    if grant is not None:
        overlay(**grant)
    script = Script({"final": "done"})
    monkeypatch.setattr(server, "_make_tier_generate", script.make)
    assert server._agent_turn("list files", tier=DENIED, project=str(project)) == BLOCKED % DENIED
    assert (BLOCKED % DENIED).lower() in server.agent("list files", tier=DENIED, project=str(project)).lower()
    assert script.prompts == []


def test_a_direct_agent_impl_call_never_opens_a_restricted_lane(catalog, overlay, project, monkeypatch):
    overlay()
    script = Script({"final": "done"})
    monkeypatch.setattr(server, "_make_tier_generate", script.make)
    assert server._agent_impl("list", tier=DENIED, project=str(project)) == BLOCKED % DENIED
    assert script.prompts == []


def test_suppression_wins_over_an_inner_entry(catalog, overlay, project, monkeypatch):
    overlay()
    script = Script({"final": "done"})
    monkeypatch.setattr(server, "_make_tier_generate", script.make)
    with lane.suppressed():
        output = server.workbench_agent("inspect", tier=DENIED, project=str(project))
    assert (BLOCKED % DENIED).lower() in output.lower()
    assert script.prompts == []


def test_unsafe_lab_keeps_the_refusal(catalog, overlay, project, monkeypatch):
    overlay()
    monkeypatch.setattr(server.unsafe_lab, "active", lambda: True)
    script = Script({"final": "done"})
    monkeypatch.setattr(server, "_make_tier_generate", script.make)
    entry = lane.LaneEntry("agent", str(project))
    assert server._agent_turn("x", tier=DENIED, project=str(project), restricted_lane=entry) == BLOCKED % DENIED
    assert script.prompts == []


def test_the_loop_action_never_opens_a_restricted_lane(catalog, overlay, project, monkeypatch):
    overlay()
    seen = []
    monkeypatch.setattr(server, "agent", lambda **kwargs: seen.append(lane.entry("agent", kwargs.get("project"))) or "ok")
    monkeypatch.setattr(server, "_loop_permission_refusal", lambda *a, **k: None)
    server._loop_dispatch({"type": "agent", "task": "x", "tier": DENIED, "project": str(project)})
    assert seen == [None]


# ---- every other lane still refuses ------------------------------------------------

@pytest.fixture
def granted(catalog, overlay):
    overlay()
    assert lane.decide(DENIED, server._RUNTIME_POLICY).permits("agent")


def test_codegen_build_loop_still_refuses(granted, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(server, "_make_tier_generate", lambda *a, **k: started.append(a))
    files = json.dumps({"a.c": "one C file"})
    assert server.codegen_build_loop(str(tmp_path), files, "build", tiers=DENIED) == BLOCKED % DENIED
    monkeypatch.setitem(server.TIERS, "code", DENIED)
    assert server.codegen_build_loop(str(tmp_path), files, "build", tiers="code") == BLOCKED % DENIED
    assert started == []


def test_consult_by_name_still_executes_nothing(granted, monkeypatch):
    """Chat by name stays text only: a grant adds no code execution to consult."""
    runs, asked = [], []
    monkeypatch.setattr(server, "_code_gate_enabled", lambda: True)
    monkeypatch.setattr(server.grounding, "run_code_detail", lambda code, **k: runs.append(code))
    monkeypatch.setattr(server.grounding, "run_code", lambda *a, **k: runs.append(a))
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)

    def make(tier, model, *args, **kwargs):
        return lambda prompt, history=None, **kw: asked.append(model) or RUNNABLE

    monkeypatch.setattr(server, "_make_tier_generate", make)
    out = server.consult("q", tiers=DENIED)
    assert asked == [DENIED] and runs == []
    assert "blocked from tool-using" not in out


def test_the_chat_code_gate_never_runs_a_restricted_models_reply(granted, monkeypatch):
    runs = []
    monkeypatch.setattr(server, "_code_gate_enabled", lambda: True)
    monkeypatch.setattr(server.grounding, "run_code_detail", lambda code, **k: runs.append(code))
    monkeypatch.setattr(server.grounding, "run_code", lambda *a, **k: runs.append(a))
    common = {"iid": None, "prompt": "p", "history": [], "system": "", "temperature": 0.2,
              "num_predict": 64, "num_ctx": 0, "cloud": False}
    for label in ("model:" + DENIED, "general"):
        assert server._gate_answer_code(RUNNABLE, model=DENIED, tier_label=label, **common) == (RUNNABLE, None, False, {})
    assert runs == []


def test_tool_lane_guard_lanes_still_refuse(granted):
    policy = server._RUNTIME_POLICY
    assert guard.denied(DENIED, "model:" + DENIED, policy)
    assert guard.resolved_refusal(DENIED, None, guard.DENIED_LABEL, policy) == BLOCKED % DENIED
    assert guard.selector_refusal(DENIED, dict(server.TIERS), policy) == BLOCKED % DENIED
    assert guard.sweep_skip_reason(DENIED, policy)
    assert server._serve_target(DENIED, None)[3] == guard.DENIED_LABEL
    # The agent turn itself, entered without the agent lane, refuses as today.
    assert server._agent_turn("x", tier=DENIED) == BLOCKED % DENIED


def test_the_default_route_still_refuses_a_restricted_model_in_a_tier(granted):
    refused = guard.default_route_refusal({**server.TIERS, "general": DENIED}, server._RUNTIME_POLICY)
    assert DENIED in refused and "blocked" in refused


def test_mesh_agent_tools_still_refuse(granted, tmp_path):
    from types import SimpleNamespace

    from sonder_runtime.bootstrap import mesh_agent_tools
    from sonder_runtime.domain.common.errors import SonderError

    runner = SimpleNamespace(locate_blender=lambda: str(tmp_path / "blender.exe"))
    rt = SimpleNamespace(TIERS={"general": DENIED, "code": ALLOWED}, _RUNTIME_POLICY=server._RUNTIME_POLICY)
    with pytest.raises(SonderError, match="blocked from tool-using lanes"):
        mesh_agent_tools.generate_mesh("a crate", tiers="general", rt=rt, runner=runner, output_dir=str(tmp_path))


def test_no_other_lane_reads_the_restricted_decision():
    """Only the agent turn, the console editor and this lane's own modules may ask for a grant."""
    allowed = {
        "server.py", "sonder_runtime/adapters/restricted_tool_lane.py",
        "sonder_runtime/adapters/model_tool_policy_editor.py",
        "sonder_runtime/adapters/chat_file_routing.py",
        "sonder_runtime/adapters/model_tool_grants.py",  # its docstring names the lane
        "sonder_runtime/bootstrap/prepared_workbench.py",  # suppresses it
    }
    readers = set()
    for path in [REPO / "runtime" / "server.py", *REPO.glob("sonder_runtime/**/*.py"), *REPO.glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        if "restricted_tool_lane" in text and path.name != "test_restricted_tool_lane.py":
            readers.add(path.relative_to(REPO).as_posix())
    assert readers == allowed, readers
    tree = ast.parse((REPO / "runtime" / "server.py").read_text(encoding="utf-8"))
    admitters = {
        function.name for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        and any(
            isinstance(node, ast.Attribute) and node.attr == "admit"
            and isinstance(node.value, ast.Name) and node.value.id == "_restricted_tool_lane"
            for node in ast.walk(function)
        )
    }
    assert admitters == {"_agent_turn"}


# Every in-process caller of the two entry points, by enclosing function, and
# why each is safe.  A new caller runs ``agent`` or ``workbench_agent`` on
# behalf of some lane: decide whether it may open a restricted lane (wrap it in
# ``restricted_tool_lane.suppressed()`` if not), then add it here.
ENTRY_CALLERS = {
    # /work without a tier: "auto", the escalating path, which is suppressed.
    ("server.py", "control_command", "workbench_agent"),
    # The REPL's /work with the operator's own explicit tier: the
    # workbench_agent lane, only when the profile lists it.
    ("server.py", "_run_managed_repl_work", "workbench_agent"),
    # A model-planned loop action: suppressed.
    ("server.py", "_loop_dispatch", "workbench_agent"),
    # Escalation rungs: inside the suppressed _workbench_agent_escalating.
    ("server.py", "_workbench_agent_escalating_owned", "workbench_agent"),
    # The explicit-tier branch enters the workbench_agent lane.
    ("server.py", "workbench_agent", "agent"),
    # Chat file routing with a pinned model: suppressed.
    ("sonder_runtime/adapters/chat_file_routing.py", "route_file_request", "workbench_agent"),
    # A prepared plan from another surface: suppressed.
    ("sonder_runtime/bootstrap/prepared_workbench.py", "execute_prepared_workbench", "agent"),
    # HTTP /work: tier "auto", the suppressed escalating path.
    ("sonder_runtime/interfaces/http/serve.py", "_handle_slash", "workbench_agent"),
}


def _entry_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute) and not (
                    isinstance(func.value, ast.Name) and func.value.id == "self"
                ):
                    name = func.attr  # host.agent, server.workbench_agent, self.runtime.agent
                else:
                    name = ""
                if name in ("agent", "workbench_agent"):
                    found.add((path.relative_to(REPO).as_posix(), scope, name))
            visit(child, inner)

    visit(tree, "<module>")
    return found


def test_every_caller_of_an_entry_point_is_reviewed():
    calls = set()
    for path in [*REPO.glob("*.py"), *REPO.glob("sonder_runtime/**/*.py")]:
        calls |= _entry_calls(path)
    assert calls == ENTRY_CALLERS, sorted(calls ^ ENTRY_CALLERS)


def test_the_auto_tier_path_is_suppressed(catalog, overlay, project, monkeypatch):
    """/work's "auto" path escalates over tiers; its rungs never open a restricted lane."""
    overlay()
    seen = []
    monkeypatch.setattr(server, "agent", lambda **kwargs: seen.append(lane.entry("agent", kwargs.get("project"))) or "ok")
    monkeypatch.setattr(server, "_local_tier_rung", lambda tier: None)
    server._workbench_agent_escalating(
        "inspect", "general", max_steps=2, allow_web=False, project=str(project), allow_location=False,
    )
    assert seen == [None]


# ---- the restricted model's own calls reach the real generation guard -------------
# Above, the generator is scripted; here only the HTTP transport is, so every
# call goes through _make_generate, _chat_request and require_generation.

@pytest.fixture
def transport(monkeypatch):
    """Fake Ollama transport: each send answers with the next scripted decision."""
    sent, replies = [], []

    def fake_post(path, payload, **kwargs):
        sent.append(payload["model"])
        content = replies.pop(0) if replies else json.dumps({"final": "done"})
        return {
            "model": payload["model"],
            "message": {"role": "assistant", "content": content},
            "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 2,
        }, 1

    monkeypatch.setattr(server, "_post_model", fake_post)
    monkeypatch.setattr(server, "_known_thinking_model", lambda model: False)
    monkeypatch.setattr(server, "_think_option_supported", lambda model: False)
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 4096)
    return sent, replies


def test_a_restricted_turn_reaches_its_model_through_the_real_guard(
    catalog, overlay, project, transport, dispatched,
):
    overlay()
    sent, replies = transport
    replies.extend([
        json.dumps({"tool": "text_search", "args": {"query": "needle"}}),
        json.dumps({"final": "found it"}),
    ])
    output = server.agent("inspect the project", tier=DENIED, checklist=False, project=str(project))
    assert "blocked from tool-using" not in output, output
    assert sent and set(sent) == {DENIED}, sent
    assert [name for name, _args in dispatched] == ["text_search"]
    assert "found it" in output and "TOPSECRET" not in output


def test_the_same_model_is_refused_outside_the_restricted_turn(catalog, overlay, project, transport):
    overlay()
    sent, _replies = transport
    server.agent("inspect the project", tier=DENIED, checklist=False, project=str(project))
    before = len(sent)
    gen = server._make_generate(DENIED, "", 0.2, 64, 4096)
    with pytest.raises(server.ModelCallError) as caught:
        gen("hi")
    assert caught.value.tool_lane_refused is True
    assert len(sent) == before  # nothing sent
    with pytest.raises(server.ModelCallError):
        guard.require_generation(DENIED)


def _run(lane_name="agent", model=DENIED):
    return lane.RestrictedRun(
        lane=lane_name, model=model, tools=frozenset(GRANTED), profiles=("security-advisers",),
        secret_globs=(".env",), caller_project="", target=("x", False, None, "model:" + model),
    )


def _guarded(model, **kwargs):
    guard.require_generation(model, **kwargs)
    return "sent"


def test_the_permit_covers_only_the_runs_own_model_during_its_own_call(catalog, overlay):
    overlay()
    run = _run()
    assert run.generator(_guarded)(DENIED) == "sent"
    # Another model under the same profile is not this run's model.
    with pytest.raises(server.ModelCallError):
        run.generator(_guarded)(DENIED_SMALL)
    # After the call returns, nothing is permitted.
    with pytest.raises(server.ModelCallError):
        _guarded(DENIED)
    # Not through a bridged rung either.
    rung = type("Rung", (), {"provider": "openrouter", "tier": "code"})()
    with pytest.raises(server.ModelCallError):
        run.generator(lambda: _guarded(DENIED, rung=rung))()


def test_the_permit_does_not_follow_a_call_onto_another_thread(catalog, overlay):
    overlay()
    outcome = []

    def on_another_thread():
        def target():
            try:
                outcome.append(_guarded(DENIED))
            except server.ModelCallError:
                outcome.append("refused")
        worker = threading.Thread(target=target)
        worker.start()
        worker.join()

    _run().generator(on_another_thread)()
    assert outcome == ["refused"]


def test_the_permit_is_decided_again_on_each_call(catalog, overlay):
    overlay()
    run = _run()
    generate = run.generator(_guarded)
    assert generate(DENIED) == "sent"
    overlay(tools=[])  # the operator revokes the grant mid-turn
    with pytest.raises(server.ModelCallError):
        generate(DENIED)


def test_revoking_one_of_the_runs_tools_stops_its_next_model_call(catalog, overlay):
    overlay()
    generate = _run().generator(_guarded)
    assert generate(DENIED) == "sent"
    overlay(tools=[tool for tool in GRANTED if tool != "text_search"])  # one tool revoked mid-turn
    with pytest.raises(server.ModelCallError):
        generate(DENIED)


def test_revoking_one_of_the_runs_tools_refuses_it_at_dispatch(catalog, overlay, project):
    overlay()
    run = _run()
    run.project_root = str(project)
    check = run.tool_policy(None, lambda tool_name, args, root: args)
    assert check("text_search", {"query": "needle"}) == ""
    overlay(tools=[tool for tool in GRANTED if tool != "text_search"])
    refused = check("text_search", {"query": "needle"})
    assert "HOST POLICY" in refused and "text_search" in refused
    assert check("file_read_range", {"path": "src/app.py"}) == ""  # still granted
    overlay(tools=[])  # everything revoked
    assert "HOST POLICY" in check("file_read_range", {"path": "src/app.py"})


def test_a_tool_granted_later_does_not_join_a_running_turn(catalog, overlay, project):
    overlay(tools=["file_read_range"])
    run = lane.RestrictedRun(
        lane="agent", model=DENIED, tools=frozenset({"file_read_range"}), profiles=("security-advisers",),
        secret_globs=(".env",), caller_project="", target=("x", False, None, "model:" + DENIED),
    )
    run.project_root = str(project)
    overlay()  # the operator grants more tools mid-turn
    check = run.tool_policy(None, lambda tool_name, args, root: args)
    assert "HOST POLICY" in check("text_search", {"query": "needle"})
    assert check("file_read_range", {"path": "src/app.py"}) == ""


def test_a_secret_glob_added_mid_turn_applies_at_the_next_dispatch_and_filter(catalog, overlay, project):
    overlay()
    (project / "vault.kdbx").write_text("needle TOPSECRET\n", encoding="utf-8")
    run = _run()
    run.project_root = str(project)
    check = run.tool_policy(None, lambda tool_name, args, root: args)
    tree = "\n".join(["directory tree: %s" % project, "  [D] src", "  [F] vault.kdbx (17 bytes)"])
    search = "\n".join([
        "matches for 'needle':",
        "  vault.kdbx:1:1: needle TOPSECRET",
        "  src/app.py:1:5: def needle():",
    ])
    assert check("file_read_range", {"path": "vault.kdbx"}) == ""
    assert "vault.kdbx" in run.filter_observation("directory_tree", {"path": str(project)}, tree)
    overlay(restricted_secret_path_globs=["*.kdbx"])  # the operator adds a glob mid-turn
    refused = check("file_read_range", {"path": "vault.kdbx"})
    assert "HOST POLICY" in refused and "*.kdbx" in refused, refused
    assert "HOST POLICY" in check("text_search", {"query": "needle", "glob": "*.kdbx"})
    filtered_tree = run.filter_observation("directory_tree", {"path": str(project)}, tree)
    assert "vault.kdbx" not in filtered_tree and "  [D] src" in filtered_tree.split("\n"), filtered_tree
    filtered = run.filter_observation("text_search", {"root": str(project)}, search)
    assert "TOPSECRET" not in filtered and "  src/app.py:1:5: def needle():" in filtered.split("\n"), filtered
    # Control: an ordinary file is still read, and the glob captured at admission still applies.
    assert check("file_read_range", {"path": "src/app.py"}) == ""
    assert "HOST POLICY" in check("file_read_range", {"path": ".env"})


def test_the_filter_withholds_everything_when_the_current_globs_cannot_be_read(catalog, overlay, project, monkeypatch):
    overlay()
    run = _run()
    run.project_root = str(project)
    search = "\n".join(["matches for 'needle':", "  src/app.py:1:5: def needle():"])
    assert "app.py" in run.filter_observation("text_search", {"root": str(project)}, search)
    monkeypatch.setattr(lane._grants, "grant_set", lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable")))
    assert "app.py" not in run.observation_filter("text_search", {"root": str(project)})(search)
    check = run.tool_policy(None, lambda tool_name, args, root: args)
    assert "HOST POLICY" in check("file_read_range", {"path": "src/app.py"})


def test_the_permit_needs_the_runs_lane_in_the_grant(catalog, overlay):
    overlay(lanes=["agent"])
    with pytest.raises(server.ModelCallError):
        _run("workbench_agent").generator(_guarded)(DENIED)


def test_a_nested_agent_inside_a_tool_dispatch_gets_no_permit(catalog, overlay, project, transport, monkeypatch):
    """A tool dispatched by the restricted turn runs outside the generator's call."""
    overlay()
    sent, replies = transport
    seen = []
    real = server._agent_dispatch_observed

    def dispatch(tool_name, args, *a, **k):
        try:
            seen.append(_guarded(DENIED))
        except server.ModelCallError:
            seen.append("refused")
        return real(tool_name, args, *a, **k)

    monkeypatch.setattr(server, "_agent_dispatch_observed", dispatch)
    replies.extend([json.dumps({"tool": "directory_tree", "args": {}}), json.dumps({"final": "ok"})])
    server.agent("inspect the project", tier=DENIED, checklist=False, project=str(project))
    assert seen == ["refused"]


# ---- a junction inside the project does not reach outside it -----------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows only")


@pytest.fixture
def junctioned(project, tmp_path):
    """``<project>/docs`` is a junction to a folder outside the project."""
    import _winapi

    outside = tmp_path / "outside_project"
    outside.mkdir()
    (outside / "notes.txt").write_text("needle OUTSIDE_CONTENT\n", encoding="utf-8")
    (outside / "outside_mod.py").write_text("def outside_symbol_name():\n    return 1\n", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(project / "docs"))
    return project


@windows_only
@pytest.mark.parametrize("call", [
    {"tool": "text_search", "args": {"query": "needle", "root": "."}},
    {"tool": "repository_symbol_index", "args": {"path": "."}},
    {"tool": "directory_tree", "args": {"path": "."}},
])
def test_a_project_junction_does_not_leak_outside_content(catalog, overlay, junctioned, monkeypatch, call):
    overlay()
    script = Script(call, {"final": "done"})
    run(monkeypatch, script, project=str(junctioned))
    seen = "\n".join(script.prompts[1:])
    assert seen
    for leaked in ("OUTSIDE_CONTENT", "notes.txt", "outside_symbol_name", "outside_mod"):
        assert leaked not in seen, (leaked, seen)


class _EntryWithoutJunctionCheck:
    """A ``DirEntry`` as Python 3.11 has it: no ``is_junction``."""

    def __init__(self, entry):
        self._entry = entry
        self.name, self.path = entry.name, entry.path

    def is_symlink(self):
        return self._entry.is_symlink()

    def is_dir(self, *, follow_symlinks=True):
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def stat(self, *, follow_symlinks=True):
        return self._entry.stat(follow_symlinks=follow_symlinks)


@windows_only
def test_a_junction_is_a_link_without_direntry_is_junction(junctioned):
    """``DirEntry.is_junction`` is Python 3.12+; the walk must not need it (README: 3.11+)."""
    import os

    from sonder_runtime.adapters.filesystem import workbench

    entries = {entry.name: entry for entry in os.scandir(junctioned)}
    assert workbench._reparse_entry(_EntryWithoutJunctionCheck(entries["docs"])) is True
    assert workbench._reparse_entry(_EntryWithoutJunctionCheck(entries["src"])) is False


@windows_only
def test_directory_tree_skips_a_junction_without_direntry_is_junction(junctioned, monkeypatch):
    import os

    from sonder_runtime.adapters.filesystem import workbench

    real_scandir = os.scandir

    class _Listing:
        def __init__(self, path):
            self._inner = real_scandir(path)

        def __iter__(self):
            return (_EntryWithoutJunctionCheck(entry) for entry in self._inner)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()

    monkeypatch.setattr(workbench.os, "scandir", _Listing)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(junctioned))
    tree = workbench.directory_tree(str(junctioned), depth=3, include_ignored=True)
    names = {entry["name"] for entry in tree["entries"]}
    assert "app.py" in names
    assert "notes.txt" not in names and "outside_mod.py" not in names and "docs" not in names


@windows_only
def test_the_search_filter_withholds_a_row_reached_through_a_junction(junctioned):
    run = _run()
    run.project_root = str(junctioned)
    observation = "\n".join([
        "matches for 'needle':",
        r"  docs\notes.txt:1:1: needle OUTSIDE_CONTENT",  # spelled inside, reached through the junction
        r"  src\app.py:1:5: def needle():",
    ])
    filtered = run.filter_observation("text_search", {"root": str(junctioned)}, observation)
    assert "OUTSIDE_CONTENT" not in filtered
    assert r"  src\app.py:1:5: def needle():" in filtered.split("\n")


# ---- the in-flight filter fails closed when the files stop being trustworthy ----------------

def _search(_project):
    return "\n".join(["matches for 'needle':", "  src/app.py:1:5: def needle():"])


def test_the_filter_withholds_everything_when_the_overlay_turns_invalid_mid_turn(catalog, overlay, project):
    overlay()
    run = _run()
    run.project_root = str(project)
    search = _search(project)
    assert run.current_secret_globs() is not None
    assert "app.py" in run.filter_observation("text_search", {"root": str(project)}, search)
    # Between dispatch and filter, the overlay becomes invalid JSON (a write nobody guarded).
    (project.parent / "overlay" / "model_tool_policy.json").write_text("{bad", encoding="utf-8")
    assert run.current_secret_globs() is None
    filtered = run.filter_observation("text_search", {"root": str(project)}, search)
    assert "withheld" in filtered and "app.py" not in filtered, filtered
    # Control: with a valid overlay again, results flow.
    overlay()
    assert run.current_secret_globs() is not None
    assert "app.py" in run.filter_observation("text_search", {"root": str(project)}, search)


def test_the_filter_withholds_everything_when_the_shipped_file_changes_mid_turn(
    catalog, overlay, project, monkeypatch, tmp_path,
):
    import shutil

    copy = tmp_path / "pkg" / "model_tool_policy.json"
    copy.parent.mkdir()
    shutil.copyfile(mtp.SHIPPED_PATH, copy)
    monkeypatch.setattr(mtp, "SHIPPED_PATH", copy)
    mtp.clear_cache()
    overlay()
    run = _run()
    run.project_root = str(project)
    search = _search(project)
    assert run.current_secret_globs() is not None  # the shipped file is read (and pinned) here
    assert "app.py" in run.filter_observation("text_search", {"root": str(project)}, search)
    document = json.loads(copy.read_text(encoding="utf-8"))
    document["restricted_secret_path_globs"] = [".env"]  # a rewrite that drops most shipped secret globs
    copy.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    assert run.current_secret_globs() is None
    filtered = run.filter_observation("text_search", {"root": str(project)}, search)
    assert "withheld" in filtered and "app.py" not in filtered, filtered
