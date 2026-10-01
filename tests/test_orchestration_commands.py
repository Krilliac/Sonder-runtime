"""Pin the app's orchestration syntax and trusted choice metadata."""
from types import SimpleNamespace

import pytest

from sonder_runtime.interfaces.orchestration_commands import (
    CommandReply, execute_master_command, master_choice, parse_master_arguments,
    reply_receipt_fields, uses_tool_arguments,
)


@pytest.mark.parametrize(("argument", "mode", "count", "task"), [
    ("make me something cool", "ask", 0, "make me something cool"),
    ("fleet 0 make me something cool", "fleet", 0, "make me something cool"),
    ("fleet make me something cool", "fleet", 0, "make me something cool"),
    ("fleet 4 write primes.py", "fleet", 4, "write primes.py"),
    ("delegate 3 write primes.py", "delegate", 3, "write primes.py"),
    ("inline write primes.py", "inline", 0, "write primes.py"),
    ("inline 0 write primes.py", "inline", 0, "write primes.py"),
    ("0 write primes.py", "ask", 0, "write primes.py"),
    ("WORKFLOW 2 keep 20 numbers", "fleet", 2, "keep 20 numbers"),
    ("delagte task with 48 numbers", "delegate", 0, "task with 48 numbers"),
])
def test_parse_mode_count_and_task(argument, mode, count, task):
    parsed = parse_master_arguments(argument)
    assert (parsed.mode, parsed.agents, parsed.task) == (mode, count, task)


@pytest.mark.parametrize("argument", ["", "fleet", "fleet 0", "inline 3", "fleet -1 task"])
def test_invalid_or_empty_command_never_dispatches(argument):
    calls = []
    reply = execute_master_command(argument, orchestrate=lambda **kw: calls.append(kw),
                                   capacity=lambda: {"worker_slots": 1})
    assert calls == []
    assert "usage:" in reply or "positive" in reply


@pytest.mark.parametrize("argument", ["fleet task", "fleet 0 task", "delegate task", "delegate 0 task"])
def test_auto_count_is_capacity_sized_before_legacy_host(argument):
    calls = []
    execute_master_command(argument, orchestrate=lambda **kw: calls.append(kw),
                           capacity=lambda: {"worker_slots": 1, "max_agents": 48}, project="C:/project")
    assert calls[0]["agents"] == 1
    assert calls[0]["project"] == "C:/project"
    assert calls[0]["task"] == "task"


def test_explicit_count_does_not_probe_or_replace_host_capacity():
    calls = []
    execute_master_command("fleet 8 task", orchestrate=lambda **kw: calls.append(kw),
                           capacity=lambda: pytest.fail("unexpected capacity probe"))
    assert calls[0]["agents"] == 8


@pytest.mark.parametrize("task", [
    'write a script, then save it as "primes.py"\nand verify it',
    '20 reasons to use Python',
])
def test_choice_commands_roundtrip_original_task(task):
    reply = master_choice(task, 3, 2, 2)
    assert isinstance(reply, str)
    assert "mode=" not in reply and "Call master_orchestrate" not in reply
    data = reply_receipt_fields(reply)["orchestration"]
    assert data["worker_slots"] == 2
    for choice in data["choices"]:
        parsed = parse_master_arguments(choice["command"].split(None, 1)[1])
        assert parsed.task == task
    assert reply_receipt_fields(str(reply)) == {}
    assert reply_receipt_fields(SimpleNamespace(receipt_fields={"orchestration": data})) == {}


@pytest.mark.parametrize("argument", ['task="hello" mode=fleet', '{"task":"hello"}', "mode=inline task=hello"])
def test_structured_tool_syntax_preserved(argument):
    assert uses_tool_arguments(argument)


def test_leading_assignment_in_task_is_not_mistaken_for_tool_arguments():
    argument = "x=1 should be written into example.py"
    assert not uses_tool_arguments(argument)
    assert parse_master_arguments(argument).task == argument


@pytest.mark.parametrize("command", ["/master", "/master_orchestrate"])
@pytest.mark.parametrize("arguments,count", [("fleet 0 task", 1), ("fleet task", 1), ("fleet 4 task", 4)])
def test_http_slash_both_spellings_use_parser(monkeypatch, command, arguments, count):
    import sonder_runtime.interfaces.http.serve as serve
    calls = []
    monkeypatch.setattr(serve, "_http_slash_refusal", lambda *a, **kw: "")
    monkeypatch.setattr(serve, "_account_task_boundary_refusal", lambda *a, **kw: "")
    monkeypatch.setattr(serve, "_LEGACY_RUNTIME", SimpleNamespace(
        master_orchestrator=SimpleNamespace(capacity=lambda: {"worker_slots": 1}),
        master_orchestrate=lambda **kw: calls.append(kw) or "started",
    ))
    assert serve._handle_slash(f"{command} {arguments}", project="project") == "started"
    assert calls == [{"task": "task", "mode": "fleet", "agents": count, "project": "project"}]


def test_native_control_command_uses_same_parser(monkeypatch):
    import server
    calls = []
    monkeypatch.setattr(server, "_control_tool_refusal", lambda *a, **kw: "")
    monkeypatch.setattr(server.master_orchestrator, "capacity", lambda: {"worker_slots": 2})
    monkeypatch.setattr(server, "master_orchestrate", lambda **kw: calls.append(kw) or "started")
    assert server.control_command("/master_orchestrate fleet 0 task", project="project") == "started"
    assert calls == [{"task": "task", "mode": "fleet", "agents": 2, "project": "project"}]


def test_native_delegate_reaches_durable_lane_boundary(monkeypatch):
    import server
    from sonder_runtime.interfaces.http import agent_work_routes

    calls = []
    monkeypatch.setattr(server, "_control_tool_refusal", lambda *a, **kw: "")
    monkeypatch.setattr(agent_work_routes, "native_delegate_reply",
                        lambda task, **kw: calls.append((task, kw)) or "agent started")
    assert server.control_command("/delegate write primes.py", project="selected", session="chat-1") == "agent started"
    assert calls[0][0] == "write primes.py"
    assert calls[0][1]["project"] == "selected"
    assert calls[0][1]["parent_session_id"] == "chat-1"
    assert calls[0][1]["context_of"] is server._agent_lane_context


def test_only_trusted_reply_carries_additive_lane_metadata():
    reply = CommandReply("Agent started", agent_lane={"lane_id": "lane-1", "folder": "creations/lane-1"})
    assert reply_receipt_fields(reply)["agent_lane"]["lane_id"] == "lane-1"
    assert reply_receipt_fields("Agent started") == {}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("command", ["/master", "/master_orchestrate"])
def test_choice_receipt_survives_real_http_response(monkeypatch, stream, command):
    """The app needs trusted choices in both JSON and terminal SSE receipts."""
    import json

    import sonder_runtime.interfaces.http.serve as serve
    from tests.test_serve_auth import _http_server, _request

    monkeypatch.setattr(serve, "API_KEY", "")
    monkeypatch.setattr(serve, "AUTH_MODE", "local-open")
    monkeypatch.setattr(serve, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(serve.server, "master_orchestrate", lambda task, **kw: master_choice(task, 3, 1, 1))
    monkeypatch.setattr(serve.server, "answer_with_history", lambda *a, **kw: pytest.fail("command became model chat"))
    body = json.dumps({"model": "sonder", "stream": stream,
                       "messages": [{"role": "user", "content": f"{command} write primes.py"}]})
    with _http_server(monkeypatch) as port:
        status, _, raw = _request(port, "POST", "/v1/chat/completions", body=body,
                                  headers={"Content-Type": "application/json"})
    assert status == 200, raw
    if stream:
        frames = [json.loads(line[6:]) for line in raw.decode().splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        receipts = [frame["sonder_receipt"] for frame in frames if "sonder_receipt" in frame]
        assert len(receipts) == 1
        receipt = receipts[0]
    else:
        receipt = json.loads(raw)["sonder_receipt"]
    choices = receipt["orchestration"]["choices"]
    assert [parse_master_arguments(choice["command"].split(None, 1)[1]).task for choice in choices] == [
        "write primes.py", "write primes.py", "write primes.py",
    ]
    assert receipt["orchestration"]["fleet_agents"] == 1


@pytest.mark.parametrize("command", ["/master", "/master_orchestrate", "/delegate"])
def test_new_commands_cannot_bypass_plan_mode(monkeypatch, command):
    import permission_modes as modes
    import sonder_runtime.interfaces.http.serve as serve

    monkeypatch.setattr(modes, "_LOADED", True)
    monkeypatch.setitem(modes._STATE, "mode", modes.PLAN)
    monkeypatch.setattr(modes, "_rule_lookup", lambda tool: None)
    monkeypatch.setattr(serve.server, "master_orchestrate", lambda **kw: pytest.fail("plan dispatched work"))
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: pytest.fail("plan constructed lane service"))
    reply = serve._handle_slash(f"{command} write primes.py")
    assert reply and "refused" in reply.lower()
