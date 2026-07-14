"""Cloud-only request routing for subordinate generation steps."""

import pytest

import server


BAD_REPLY = (
    "Broken example:\n\n```python\n"
    "def parse(value):\n    return missing(value)\n\n"
    "print(parse('P3D'))\n"
    "```\n"
)
FIXED_REPLY = (
    "Corrected example:\n\n```python\n"
    "def parse(value):\n    return value.strip('P')\n\n"
    "print(parse('P3D'))\n"
    "```\n"
)


class _Connection:
    def close(self):
        return None


def _install_request_fakes(monkeypatch, *, model, cloud, tier_label):
    calls = []
    titles = []
    responses = iter((BAD_REPLY, "Routed title", FIXED_REPLY))

    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "control_command", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_route_chat_web", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_web_denial_guard", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda requested_tier, strict: (model, cloud, False, tier_label),
    )
    monkeypatch.setattr(server, "_build_system", lambda *args: "")
    monkeypatch.setattr(server, "_open_db", _Connection)

    monkeypatch.setattr(server.memory_store, "session_turn_count", lambda *args: 0)
    monkeypatch.setattr(server.memory_store, "touch_session", lambda *args: None)
    monkeypatch.setattr(
        server.memory_store, "session_turns_for_project", lambda *args: [],
    )
    monkeypatch.setattr(
        server.memory_store, "get_session_project_summary", lambda *args: {},
    )
    monkeypatch.setattr(server.memory_store, "get_interaction", lambda *args: {})
    monkeypatch.setattr(server.memory_store, "get_session", lambda *args: {})
    monkeypatch.setattr(
        server.memory_store, "set_session_title",
        lambda conn, session_id, title: titles.append(title),
    )

    def make_generate(
        selected_model, system, temperature, num_predict, num_ctx,
        cloud=False, timeout=None, cancel_check=None,
    ):
        def generate(prompt, history=None):
            calls.append({"model": selected_model, "cloud": cloud, "prompt": prompt})
            generate.last_usage = {
                "tokens_in": 1, "tokens_out": 1, "token_source": "estimated",
            }
            return next(responses)

        generate.last_usage = {}
        return generate

    monkeypatch.setattr(server, "_make_generate", make_generate)

    def answer(
        conn, prompt, selected_model, effective_system, temperature, num_predict,
        num_ctx, session_id, project, history, trace=False, tier="sonder",
        cloud=False, augment=True,
    ):
        generate = server._make_generate(
            selected_model, effective_system, temperature, num_predict, num_ctx,
            cloud=cloud,
        )
        return generate(prompt, history), "iid-route", None

    monkeypatch.setattr(server, "_answer", answer)
    results = iter((
        {
            "ok": False, "timed_out": False, "stdout": "",
            "stderr": "NameError: missing", "returncode": 1,
        },
        {
            "ok": True, "timed_out": False, "stdout": "3D\n",
            "stderr": "", "returncode": 0,
        },
    ))
    monkeypatch.setattr(
        server.grounding, "run_code_detail", lambda *args, **kwargs: next(results),
    )
    monkeypatch.setattr(server, "_persist_verified_code_repair", lambda *args: True)
    return calls, titles


def test_cloud_only_route_stays_cloud_for_answer_title_and_repair(monkeypatch):
    calls, titles = _install_request_fakes(
        monkeypatch,
        model="qwen3-coder:test-cloud",
        cloud=True,
        tier_label="cloud-code",
    )

    output = server._sonder_impl_serialized(
        "write a parser", session="cloud-session", project="none",
        tier="cloud-code", strict=True,
    )

    assert "Corrected example" in output
    assert titles == ["Routed title"]
    assert [(call["model"], call["cloud"]) for call in calls] == [
        ("qwen3-coder:test-cloud", True),
        ("qwen3-coder:test-cloud", True),
        ("qwen3-coder:test-cloud", True),
    ]


def test_local_route_keeps_fast_title_behavior(monkeypatch):
    calls, titles = _install_request_fakes(
        monkeypatch,
        model="local-code-model",
        cloud=False,
        tier_label="sonder",
    )

    output = server._sonder_impl_serialized(
        "write a parser", session="local-session", project="none",
    )

    assert "Corrected example" in output
    assert titles == ["Routed title"]
    assert [(call["model"], call["cloud"]) for call in calls] == [
        ("local-code-model", False),
        (server.TIERS["fast"], False),
        ("local-code-model", False),
    ]


def test_cloud_only_route_propagates_to_overflow_summary(monkeypatch):
    calls = []
    stored = []

    def make_generate(
        model, system, temperature, num_predict, num_ctx,
        cloud=False, timeout=None, cancel_check=None,
    ):
        def generate(prompt, history=None):
            calls.append((model, cloud))
            return "cloud summary"

        generate.last_usage = {}
        return generate

    monkeypatch.setattr(server, "_make_generate", make_generate)
    monkeypatch.setattr(
        server.memory_store, "session_turns_for_project",
        lambda *args: [
            {"id": "old", "task": "old task", "response": "old response"},
            {"id": "live", "task": "live task", "response": "live response"},
        ],
    )
    monkeypatch.setattr(
        server.memory_store, "get_session_project_summary", lambda *args: {},
    )
    monkeypatch.setattr(
        server.memory_store, "update_session_project_summary",
        lambda *args: stored.append(args[3:]),
    )
    internal_generate = server._internal_generate_for_route(
        "qwen3-coder:test-cloud", True,
    )

    history = server._session_history_messages(
        _Connection(), "session", 1, project="project",
        internal_generate=internal_generate,
    )

    assert calls == [("qwen3-coder:test-cloud", True)]
    assert stored == [("cloud summary", "old")]
    assert history == [
        {"role": "system", "content": "Earlier in this conversation:\ncloud summary"},
        {"role": "user", "content": "live task"},
        {"role": "assistant", "content": "live response"},
    ]


def test_cloud_only_route_without_model_fails_closed():
    with pytest.raises(server.ModelCallError, match="no concrete cloud model") as caught:
        server._internal_generate_for_route(None, True)

    assert caught.value.kind == "configuration"
    assert caught.value.cloud is True
    assert caught.value.attempts == 0
