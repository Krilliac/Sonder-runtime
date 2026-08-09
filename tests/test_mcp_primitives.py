import asyncio

import server


def test_runtime_resources_are_discoverable_and_deterministic():
    resources = asyncio.run(server.mcp.list_resources())
    uris = [str(item.uri) for item in resources]
    expected = {
        "sonder://runtime/status",
        "sonder://runtime/diagnostics",
        "sonder://runtime/environment",
        "sonder://runtime/tools",
    }
    assert expected <= set(uris)
    assert uris == list(dict.fromkeys(uris))


def test_runtime_resources_delegate_to_live_read_only_surfaces(monkeypatch):
    monkeypatch.setattr(server, "status", lambda: "status-now")
    monkeypatch.setattr(server, "diagnostics", lambda: "diagnostics-now")
    monkeypatch.setattr(server, "environment_status", lambda: "environment-now")
    monkeypatch.setattr(server, "tool_manifest", lambda: "tools-now")
    assert server._resource_runtime_status() == "status-now"
    assert server._resource_runtime_diagnostics() == "diagnostics-now"
    assert server._resource_host_environment() == "environment-now"
    assert server._resource_tool_manifest() == "tools-now"


def test_workflow_prompts_are_discoverable_and_render_arguments():
    prompts = asyncio.run(server.mcp.list_prompts())
    names = {item.name for item in prompts}
    assert {
        "implement_repository_task",
        "review_change",
        "grounded_research",
        "debug_failure",
    } <= names

    messages = asyncio.run(server.mcp._prompt_manager.render_prompt(
        "implement_repository_task",
        {"objective": "fix parser", "project": "D:/repo"},
    ))
    text = "\n".join(str(message.content) for message in messages)
    assert "fix parser" in text
    assert "D:/repo" in text
    assert "Never claim a build or test that did not run" in text


def test_review_prompt_keeps_user_change_as_data():
    rendered = server._prompt_review_change("diff --git a/x b/x", "security")
    assert "Focus on security" in rendered
    assert "CHANGE:\ndiff --git a/x b/x" in rendered
