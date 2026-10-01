"""Integration contracts for natural-language file chat routing.

These tests exercise the adapter with host callbacks that stand in for the real
agent loop.  The callbacks retain the arguments passed by the host and perform
the actual filesystem operation where a write is admitted; this keeps the
tests independent of a live model or Ollama process while still pinning the
policy boundary that the loop must receive.
"""
from pathlib import Path
from types import SimpleNamespace

import json
import pytest

from sonder_runtime.adapters.chat_file_routing import (
    INSPECTION_TOOLS,
    route_file_request,
)


def _host(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    state_home = tmp_path / "state"
    calls = []

    def served_work_project(value):
        return str(project) if value == str(project) else ""

    def agent_loop(prompt, **kwargs):
        """Fake model loop: only the host-provided read tools are callable."""
        calls.append(("inspect", prompt, kwargs))
        assert kwargs["read_only"] is True
        assert kwargs["require_file_evidence"] is True
        assert kwargs["allow_web"] is False
        assert kwargs["tool_allowlist"] == INSPECTION_TOOLS
        assert set(kwargs["tool_allowlist"]) == {
            "workspace_inventory", "directory_tree", "file_find", "text_search",
            "file_read", "file_read_range", "repository_symbol_index",
        }
        # This is the model's final answer after its fake workspace_inventory
        # observation; no mutation tool is exposed to this turn.
        return "3 Python files; largest: big.py (42 bytes)"

    def workbench(prompt, tier, **kwargs):
        calls.append(("write", prompt, kwargs))
        destination = Path(kwargs["project"])
        assert destination.is_relative_to(project) or destination.is_relative_to(state_home / "creations")
        (destination / "primes.py").write_text(
            "print(2)\n", encoding="utf-8",
        )
        return "Saved primes.py", tier

    host = SimpleNamespace(
        unsafe_lab=SimpleNamespace(active=lambda: False),
        served_work_project=served_work_project,
        file_ops=SimpleNamespace(workspace_root=lambda: project),
        _agent_permission_gate_error=lambda *_a, **_k: "",
        directory_create=lambda path, **_k: (Path(path).mkdir(parents=True), "created")[1],
        _agent_impl=agent_loop,
        _workbench_agent_escalating=workbench,
    )
    return host, project, state_home, calls


def test_inspection_agent_receives_real_read_only_contract_and_answer(tmp_path):
    host, project, _state_home, calls = _host(tmp_path)

    answer, tier = route_file_request(
        host,
        "how many python files are in sonder_runtime, and which is biggest?",
        "inspection",
        str(project),
        "code",
    )

    assert tier == "code"
    assert "3 Python files" in answer
    assert calls[0][0] == "inspect"
    assert "file_write" not in calls[0][2]["tool_allowlist"]


def test_admitted_write_uses_actual_tool_callback_inside_selected_project(tmp_path):
    host, project, state_home, calls = _host(tmp_path)

    answer, _tier = route_file_request(
        host, "write a python script and save it as primes.py", "workbench",
        str(project), "code", state_home=state_home,
    )

    assert "Saved primes.py" in answer
    assert (project / "primes.py").read_text(encoding="utf-8") == "print(2)\n"
    assert calls[0][2]["project"] == str(project)
    assert not (state_home / "creations").exists()


def test_plan_mode_refuses_default_creation_before_directory_or_file_write(tmp_path):
    host, _project, state_home, calls = _host(tmp_path)
    host._agent_permission_gate_error = lambda *_a, **_k: "Refused by plan mode"

    answer, _tier = route_file_request(
        host, "write a python script and save it as primes.py", "workbench",
        "default", "code", state_home=state_home,
    )

    assert "Refused by plan mode" in answer
    assert not state_home.exists()
    assert calls == []


def test_unresolved_project_cannot_fall_back_to_server_cwd(tmp_path):
    host, project, state_home, calls = _host(tmp_path)

    answer, _tier = route_file_request(
        host, "edit primes.py", "workbench", "../outside", "code",
        state_home=state_home,
    )

    assert "Refused" in answer
    assert calls == []
    assert not (project / "primes.py").exists()


def test_unsafe_lab_cannot_remove_inspection_scope(tmp_path):
    host, project, state_home, calls = _host(tmp_path)
    host.unsafe_lab.active = lambda: True

    answer, _tier = route_file_request(
        host, "show me the workspace", "inspection", str(project), "code",
        state_home=state_home,
    )

    assert "Refused" in answer
    assert calls == []


@pytest.fixture
def guarded_host(monkeypatch, tmp_path):
    """Use real agent/tool gates and only replace inference with a scripted model."""
    import server
    import permission_modes

    project = tmp_path / "active-project"
    project.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_serve_target", lambda *_a: ("fixture-model", False, False, "code"))
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda *_a: None)
    monkeypatch.setattr(server, "_capability_refined_tier", lambda _p, tier, reason: (tier, reason))
    # Replace only the mode state, not permission evaluation/dispatch. Pytest
    # restores the original dict and loaded flag at fixture teardown.
    monkeypatch.setattr(permission_modes, "_STATE", {
        **permission_modes._STATE, "mode": "acceptEdits", "elevated": False,
    })
    monkeypatch.setattr(permission_modes, "_LOADED", True)
    return server, project, permission_modes


def _scripted_model(monkeypatch, host, responses, seen):
    choices = iter(responses)

    def generate(prompt, history=None):
        seen.append(prompt)
        return json.dumps(next(choices, {"final": "Stopped; no further action claimed."}))

    monkeypatch.setattr(host, "_make_generate", lambda *_a, **_kw: generate)


def test_real_agent_inspection_answers_from_fake_tool_and_rejects_mutation(
    guarded_host, monkeypatch,
):
    host, project, _mode = guarded_host
    seen = []
    inventory_calls = []

    def inventory(**kwargs):
        inventory_calls.append(kwargs)
        return "workspace inventory\n  Python files: 3\n  largest: big.py (42 bytes)"

    monkeypatch.setattr(host, "workspace_inventory", inventory)
    _scripted_model(monkeypatch, host, [
        {"tool": "file_write", "args": {"path": "intruder.py", "content": "no"}},
        {"tool": "workspace_inventory", "args": {"root": "."}},
        {"final": "There are 3 Python files; big.py is largest at 42 bytes."},
    ], seen)
    result = host.route_work_request(
        "How many Python files are in this folder and which file is biggest?", project=str(project),
    )
    assert "read-only workspace inspection" in result
    assert "There are 3 Python files" in result
    assert len(inventory_calls) == 1
    # The agent may propose either ``root`` or ``path``.  The real dispatcher
    # normalizes workspace_inventory to its canonical ``path`` argument before
    # invoking the tool, so the callback sees path here.
    assert Path(inventory_calls[0]["path"]).resolve() == project.resolve()
    assert "largest: big.py (42 bytes)" in seen[-1]
    assert not (project / "intruder.py").exists()


@pytest.mark.parametrize("project_kind", ["selected", "default"])
def test_real_write_dispatch_is_confined_to_selected_or_creation_folder(
    guarded_host, monkeypatch, tmp_path, project_kind,
):
    host, project, _mode = guarded_host
    from sonder_runtime.adapters import chat_file_routing

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(chat_file_routing.paths, "default_home", lambda: state)
    seen = []
    _scripted_model(monkeypatch, host, [
        # The real workbench checklist requires grounded inspection before a
        # mutation.  Keep the scripted model on that production path rather
        # than asking the host to accept an unsafe first-turn write.
        {"tool": "workspace_inventory", "args": {"path": "."}},
        {"tool": "file_write", "args": {"path": "primes.py", "content": "print(2)\n"}},
        {"final": "Saved primes.py; not run or verified."},
    ], seen)
    host.route_work_request(
        "Write a Python script and save it as primes.py",
        project=str(project) if project_kind == "selected" else "default",
    )
    if project_kind == "selected":
        target = project / "primes.py"
        assert not (state / "creations").exists()
    else:
        targets = list((state / "creations").glob("chat-*/primes.py"))
        assert len(targets) == 1
        target = targets[0]
        assert not (project / "primes.py").exists()
    assert target.read_text(encoding="utf-8") == "print(2)\n"


@pytest.mark.parametrize("mode,escape", [("plan", False), ("acceptEdits", True)])
def test_real_write_dispatch_refuses_permission_or_path_escape(
    guarded_host, monkeypatch, tmp_path, mode, escape,
):
    host, project, permissions = guarded_host
    monkeypatch.setitem(permissions._STATE, "mode", mode)
    outside = tmp_path / "outside.py"
    seen = []
    _scripted_model(monkeypatch, host, [
        # Reach the mutation policy after satisfying the production
        # inspect-before-mutate checklist requirement.
        {"tool": "workspace_inventory", "args": {"path": "."}},
        {"tool": "file_write", "args": {
            "path": str(outside) if escape else "primes.py", "content": "print(2)\n",
        }},
        {"final": "No file was saved."},
    ], seen)
    host.route_work_request("Save the script as primes.py", project=str(project))
    evidence = "\n".join(seen).lower()
    if mode == "plan":
        assert "tool 'file_write' is refused by the active permission gate" in evidence
    else:
        assert "agent project path rejected: path is outside the host-selected project root" in evidence
    assert not outside.exists()
    assert not (project / "primes.py").exists()


def test_http_inspection_preserves_receipts_and_authorization(guarded_host, monkeypatch, tmp_path):
    host, project, _mode = guarded_host
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository

    repository = SQLiteSessionRepository(tmp_path / "chat.sqlite")
    monkeypatch.setattr(bootstrap_app, "default_app", lambda: SimpleNamespace(session_repository=lambda: repository))
    monkeypatch.setattr(host, "_agent_impl", lambda *_a, **_kw: "Tool evidence: 3 files")
    prompt = "How many files are in this folder?"
    assert serve._handle_work_intent(prompt, project=str(project), authorized=False) is None
    result = serve._handle_work_intent(
        prompt, project=str(project), authorized=True, with_receipt=True,
        context={"mode": "local-open"}, session_id="file-chat", session_ref="file-chat",
    )
    receipt = result.public_receipt()
    assert receipt["requested_mode"] == "inspection"
    assert receipt["routing_reason"] == "read-only local workspace inspection"
    assert receipt["admission_event_id"] and receipt["return_event_id"]
    assert [event.event_type for event in repository.read_complete("file-chat")] == [
        "chat.work.admitted", "chat.work.returned",
    ]


def test_http_default_file_project_avoids_opaque_storage_namespace(monkeypatch):
    from sonder_runtime.interfaces.http import serve
    monkeypatch.setattr(serve.server, "served_work_project", lambda _p: "")
    assert serve._work_project_for_request("default", "opaque-owner-project", "save primes.py") == "default"
    assert serve._work_project_for_request("default", "opaque-owner-project", "Hello") == "opaque-owner-project"
