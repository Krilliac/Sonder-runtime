"""Comprehensive natural-language routing matrix across Sonder Runtime.

Exercises every natural-language routing surface (feedback, control intents,
REPL command_router, curated HTTP natural-command bridge, multi-model fanout /
ensemble / consult / route, web/weather, GIF/image generation, workspace
inspection/workbench/autopilot/fleet, auto-personas, and negative anti-hijack
traps) at both classifier level and end-to-end HTTP ``/v1/chat/completions``.
"""
from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import feedback
import intents
import permission_modes as pm
import personas
import server
import sonder_runtime.interfaces.http.serve as ts
import web_intents
from sonder_runtime.interfaces.repl import command_router

pytestmark = pytest.mark.unit

_PYTHON_REPLY = "Here you go:\n\n```python\nprint('hello sonder')\n```\n"
_OWNER_KEY = "nl-routing-matrix-owner-key-0123456789"
_USER_CTX = {
    "authorized": True,
    "mode": "account",
    "api_key": False,
    "account": {"role": "user", "username": "alice"},
}


@pytest.fixture(autouse=True)
def _sandbox_mode_and_sessions(tmp_path, monkeypatch):
    """Isolate permission mode and HTTP session state per test."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    saved_state = dict(pm._STATE)
    saved_loaded = pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.DEFAULT_MODE, elevated=False, elevation_reason="")
    pm._LOADED = True
    with ts._HTTP_SESSION_STATES_LOCK:
        saved_sessions = dict(ts._HTTP_SESSION_STATES)
        ts._HTTP_SESSION_STATES.clear()
    try:
        yield
    finally:
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved_state)
        pm._LOADED = saved_loaded
        with ts._HTTP_SESSION_STATES_LOCK:
            ts._HTTP_SESSION_STATES.clear()
            ts._HTTP_SESSION_STATES.update(saved_sessions)


def _trace_http_route(prompt: str) -> str:
    """Mirror the HTTP /v1/chat/completions routing precedence for a prompt."""
    fb = feedback.classify_signal(prompt) or feedback.classify_feedback(prompt)
    if fb:
        return f"feedback:{fb}"
    ctrl = intents.classify(prompt)
    if ctrl:
        if ctrl.get("run_window"):
            return "ctrl:run_window"
        if ctrl.get("run"):
            return "ctrl:run"
        if "train" in ctrl:
            return "ctrl:train"
        return "ctrl:" + ",".join(sorted(ctrl))
    nm = server.natural_model_request(prompt)
    if nm and nm.get("kind") in ("fanout", "ensemble"):
        return f"nat_model:{nm['kind']}"
    repl = command_router.resolve(prompt)
    if repl and repl.startswith("/"):
        head = repl.split(None, 1)[0].lower()
        if head in ts._HTTP_NATURAL_COMMANDS and not (
            head == "/contextsize" and " " in repl.strip()
        ):
            return f"nat_cmd:{head}"
    wi = web_intents.classify(prompt)
    if wi:
        return f"web:{wi['kind']}"
    if intents.classify_gif_generation(prompt):
        return "gif"
    if intents.classify_image_generation(prompt):
        return "image"
    ex = intents.classify_execution(prompt)
    if ex:
        return f"exec:{ex['mode']}"
    return "chat"


# ---------------------------------------------------------------------------
# 1. Classifier & precedence matrix across all lanes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt, expected_route",
    [
        # Code execution follow-ups
        ("run it", "ctrl:run"),
        ("run it so i can see it", "ctrl:run"),
        ("run it and show me the output", "ctrl:run"),
        ("try running the code you just wrote", "ctrl:run"),
        ("execute that script", "ctrl:run"),
        ("can you run the script to see what happens", "ctrl:run"),
        ("run that in a new window", "ctrl:run_window"),
        ("run it in a console", "ctrl:run_window"),
        # Control toggles & training
        ("show me your reasoning", "ctrl:trace"),
        ("turn trace off", "ctrl:trace"),
        ("strict on", "ctrl:strict"),
        ("strict on, trace off", "ctrl:strict,trace"),
        ("train on 5 tasks", "ctrl:train"),
        ("practice", "ctrl:train"),
        # Passive feedback
        ("thanks, that worked", "feedback:accepted"),
        ("no that's wrong", "feedback:rejected"),
        # Media: GIF & Image
        ("generate a gif of a spinning neon cube", "gif"),
        ("make me an animated gif of rain on glass", "gif"),
        ("create a looping gif of a rocket launching", "gif"),
        ("animate a cute pixel cat", "gif"),
        ("draw a cyberpunk city at night", "image"),
        ("generate an image of a lighthouse in a storm", "image"),
        ("make me a wallpaper of Mars", "image"),
        ("create a logo for a coffee shop", "image"),
        # Session controls: persona, thinking effort, sampling temperature
        ("set persona to security", "nat_cmd:/persona"),
        ("switch to architect persona", "nat_cmd:/persona"),
        ("use concise mode", "nat_cmd:/persona"),
        ("reset persona to auto", "nat_cmd:/persona"),
        ("list available personas", "nat_cmd:/persona"),
        ("what is my current persona?", "nat_cmd:/persona"),
        ("turn thinking off", "nat_cmd:/effort"),
        ("turn thinking on", "nat_cmd:/effort"),
        ("disable reasoning", "nat_cmd:/effort"),
        ("set reasoning effort to high", "nat_cmd:/effort"),
        ("set effort to auto", "nat_cmd:/effort"),
        ("reset effort to auto", "nat_cmd:/effort"),
        ("set temperature to 0.2", "nat_cmd:/temp"),
        ("set sampling temperature to 1.0", "nat_cmd:/temp"),
        ("reset temperature to auto", "nat_cmd:/temp"),
        ("show my sampling temperature", "nat_cmd:/temp"),
        # Status, introspection, environment, hardware
        ("show me your stats", "nat_cmd:/stats"),
        ("how much context am i using?", "nat_cmd:/context"),
        ("what gpu do i have?", "nat_cmd:/hardware"),
        ("what os are you running on?", "nat_cmd:/env"),
        ("what tools do you have?", "nat_cmd:/tool_manifest"),
        ("what version of sonder is this?", "nat_cmd:/version"),
        ("are you healthy?", "nat_cmd:/diagnostics"),
        ("check for sonder updates", "nat_cmd:/updatecheck"),
        ("show permissions", "nat_cmd:/permissions"),
        ("what commands are there?", "nat_cmd:/commands"),
        ("show my active goal", "nat_cmd:/goal"),
        ("list my saved workflows", "nat_cmd:/workflow_list"),
        # Tasks / Todos
        ("show my tasks", "nat_cmd:/todo"),
        ("add a todo to fix the login bug", "nat_cmd:/todo"),
        ("add a task to review PR 42", "nat_cmd:/todo"),
        # Multi-model consultation, routing, ensemble, fanout
        ("get a second opinion on sqlite vs postgres", "nat_cmd:/consult"),
        ("which model should handle a C++ crash dump?", "nat_cmd:/route"),
        (
            "ask several local models what causes TCP head-of-line blocking",
            "nat_cmd:/ensemble",
        ),
        ("fanout across all local models: explain RAII", "nat_model:fanout"),
        ("ask all local models: explain move semantics", "nat_model:fanout"),
        # Web & weather
        ("what's the weather in Austin?", "web:weather"),
        ("search the web for latest Python 3.14 release notes", "web:research"),
        ("do you have internet access?", "web:capability"),
        # Repository inspection (read-only)
        ("git status", "nat_cmd:/repo_status"),
        ("show me the recent commits", "nat_cmd:/repo_log"),
        ("show me the diff", "nat_cmd:/repo_diff"),
        ("show me the uncommitted changes", "nat_cmd:/repo_status"),
        # Workspace files, workbench, autopilot, fleet
        ("read README.md", "exec:inspection"),
        ("search the repo for TODO markers", "exec:inspection"),
        ("how many python files are in this project?", "exec:inspection"),
        ("run the tests", "exec:workbench"),
        ("fix the failing auth test in tests/test_auth.py", "exec:workbench"),
        ("start an autopilot plan to migrate the config loader", "exec:autopilot"),
        ("spawn a fleet of 3 agents to audit the adapters", "exec:fleet"),
        ("build a 2d space shooter game in python", "exec:workbench"),
    ],
)
def test_http_natural_language_routing_matrix(prompt, expected_route):
    assert _trace_http_route(prompt) == expected_route, prompt


@pytest.mark.parametrize(
    "prompt, expected_persona",
    [
        ("Threat model this JWT refresh endpoint", "security"),
        ("Review this diff for race conditions", "reviewer"),
        ("Design an architecture for distributed caching", "architect"),
        ("Prove that sqrt(2) is irrational in Lean 4", "mathematician"),
        ("Write me a short story about a lighthouse keeper", "creative"),
        ("ELI5 how public key cryptography works", "explainer"),
        ("Teach me Rust lifetimes step-by-step tutorial", "teacher"),
        ("TL;DR what is WebAssembly?", "concise"),
    ],
)
def test_auto_persona_prompts_stay_in_chat_without_hijack(prompt, expected_persona):
    assert _trace_http_route(prompt) == "chat", prompt
    assert personas.resolve_auto(prompt) == expected_persona, prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "how do I generate an image in Python?",
        "explain what an animated gif is",
        "animate a button on hover",
        "animate this modal with keyframes",
        "convert video.mp4 to gif using ffmpeg",
        "how do I execute shell commands in Python?",
        "explain strict mode in JavaScript",
        "what is a persona in UX design?",
        "switch to dark mode",
        "how does thinking work?",
        "turn off thinking about edge cases when prototyping",
        "set temperature to 72 degrees",
        "forecast revenue growth for Q4",
        "write a poem about a database server",
        "summarize this conversation",
        "run this by the team first",
        "git status and then push",
        "how does git diff work?",
        "how does this work?",
        "what is 17 * 23?",
        "hello sonder",
    ],
)
def test_negative_anti_hijack_prompts_stay_plain_chat(prompt):
    assert _trace_http_route(prompt) == "chat", prompt


def test_compound_work_traps_not_hijacked_by_control_or_todo():
    """Compound/engineering work must reach workbench, never ctrl:run or /todo."""
    assert intents.classify("run it and delete the database") == {}
    assert command_router.resolve("add a task runner to webpack") is None
    assert _trace_http_route("run it and delete the database") == "exec:workbench"
    assert _trace_http_route("add a task runner to webpack") == "exec:workbench"


@pytest.mark.parametrize(
    "prompt, expected_slash",
    [
        ("remember that our build uses ninja", "/fact our build uses ninja"),
        ("what do you remember?", "/facts"),
        ("improve parse_header in parser.py to avoid copies", "/refactor parser.py parse_header avoid copies"),
        ("set persona to security", "/persona security"),
        ("switch to architect persona", "/persona architect"),
        ("use concise mode", "/persona concise"),
        ("reset persona to auto", "/persona auto"),
        ("turn thinking off", "/effort off"),
        ("turn thinking on", "/effort medium"),
        ("set reasoning effort to high", "/effort high"),
        ("set temperature to 0.2", "/temp 0.2"),
        ("reset temperature to auto", "/temp auto"),
        ("how much context am i using?", "/context"),
        ("add a todo to fix the login bug", "/todo add fix the login bug"),
        ("run that in a new window", "/runwindow"),
    ],
)
def test_repl_command_router_new_and_fixed_rules(prompt, expected_slash):
    assert command_router.resolve(prompt) == expected_slash, prompt


# ---------------------------------------------------------------------------
# 2. End-to-end HTTP /v1/chat/completions dispatch & security boundaries
# ---------------------------------------------------------------------------


@contextmanager
def _http_server(monkeypatch, *, api_key: str = ""):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "API_KEY", api_key)
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key" if api_key else "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts._legacy_runtime(), "chat_web_response", lambda *a, **k: None)
    monkeypatch.setattr(
        ts._legacy_runtime(), "route_computer_use", lambda *a, **k: None,
    )
    ts._live_telemetry_application(build=True)
    warm_operations = getattr(ts.sonder_lifecycle.get(), "operations", None)
    if callable(warm_operations):
        warm_operations()
    ts.command_catalog.http_slash_tools()
    ts.command_catalog.catalog()
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post_chat(
    port: int,
    prompt: str,
    *,
    api_key: str = "",
    session: str = "nl-matrix-session",
    messages: list[dict] | None = None,
) -> str:
    payload = {
        "model": "sonder",
        "session": session,
        "messages": messages if messages is not None else [{"role": "user", "content": prompt}],
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
    response = conn.getresponse()
    status, raw = response.status, response.read()
    conn.close()
    assert status == 200, raw
    return json.loads(raw)["choices"][0]["message"]["content"]


def test_http_end_to_end_natural_commands_dispatch_without_llm(monkeypatch):
    calls: list[tuple] = []
    runtime = ts._legacy_runtime()
    monkeypatch.setattr(
        runtime,
        "answer_with_history",
        lambda *a, **k: pytest.fail(f"routed NL command must not call LLM: {a}"),
    )
    monkeypatch.setattr(runtime, "sonder_stats", lambda: calls.append(("stats",)) or "STATS_OK")
    monkeypatch.setattr(runtime, "context_health", lambda: calls.append(("context",)) or "CONTEXT_OK")
    monkeypatch.setattr(runtime, "hardware_profile", lambda **kw: calls.append(("hardware", kw)) or "HARDWARE_OK")
    monkeypatch.setattr(runtime, "environment_status", lambda **kw: calls.append(("env", kw)) or "ENV_OK")
    monkeypatch.setattr(runtime, "diagnostics", lambda: calls.append(("diagnostics",)) or "DIAG_OK")
    monkeypatch.setattr(runtime, "repo_status", lambda: calls.append(("repo_status",)) or "REPO_STATUS_OK")
    monkeypatch.setattr(runtime, "repo_log", lambda: calls.append(("repo_log",)) or "REPO_LOG_OK")
    monkeypatch.setattr(runtime, "repo_diff", lambda: calls.append(("repo_diff",)) or "REPO_DIFF_OK")
    monkeypatch.setattr(runtime, "task_list", lambda **kw: calls.append(("task_list", kw)) or "TASKS_OK")
    monkeypatch.setattr(runtime, "task_create", lambda **kw: calls.append(("task_create", kw)) or f"CREATED:{kw.get('title')}")
    monkeypatch.setattr(runtime, "route_request", lambda prompt, **kw: calls.append(("route", prompt)) or f"ROUTE:{prompt}")
    monkeypatch.setattr(runtime, "consult", lambda prompt, **kw: calls.append(("consult", prompt)) or f"CONSULT:{prompt}")
    monkeypatch.setattr(runtime, "ensemble_answer", lambda prompt, **kw: calls.append(("ensemble", prompt)) or f"ENSEMBLE:{prompt}")
    monkeypatch.setattr(
        ts._image_generation,
        "generate_chat_gif",
        lambda prompt, **kw: calls.append(("gif", prompt)) or SimpleNamespace(
            markdown=f"![gif]({prompt})", model_id="gif-studio",
        ),
    )

    with _http_server(monkeypatch, api_key=_OWNER_KEY) as port:
        # Session controls
        assert "persona: security (pinned)" in _post_chat(port, "set persona to security", api_key=_OWNER_KEY)
        assert "persona: architect (pinned)" in _post_chat(port, "switch to architect persona", api_key=_OWNER_KEY)
        assert "persona: auto" in _post_chat(port, "reset persona to auto", api_key=_OWNER_KEY)
        assert "off" in _post_chat(port, "turn thinking off", api_key=_OWNER_KEY).lower()
        assert "high" in _post_chat(port, "set reasoning effort to high", api_key=_OWNER_KEY).lower()
        assert "0.2" in _post_chat(port, "set temperature to 0.2", api_key=_OWNER_KEY)
        assert "auto" in _post_chat(port, "reset temperature to auto", api_key=_OWNER_KEY).lower()

        # Introspection & repo & todo
        assert _post_chat(port, "show me your stats", api_key=_OWNER_KEY) == "STATS_OK"
        assert _post_chat(port, "how much context am i using?", api_key=_OWNER_KEY) == "CONTEXT_OK"
        assert _post_chat(port, "what gpu do i have?", api_key=_OWNER_KEY) == "HARDWARE_OK"
        assert _post_chat(port, "what os are you running on?", api_key=_OWNER_KEY) == "ENV_OK"
        assert _post_chat(port, "what version of sonder is this?", api_key=_OWNER_KEY).startswith("sonder ")
        assert _post_chat(port, "are you healthy?", api_key=_OWNER_KEY) == "DIAG_OK"
        assert _post_chat(port, "git status", api_key=_OWNER_KEY) == "REPO_STATUS_OK"
        assert _post_chat(port, "show me the recent commits", api_key=_OWNER_KEY) == "REPO_LOG_OK"
        assert _post_chat(port, "show me the diff", api_key=_OWNER_KEY) == "REPO_DIFF_OK"
        assert _post_chat(port, "show my tasks", api_key=_OWNER_KEY) == "TASKS_OK"
        assert _post_chat(port, "add a todo to fix the login bug", api_key=_OWNER_KEY) == "CREATED:fix the login bug"

        # Multi-model & media
        assert _post_chat(port, "which model should handle a C++ crash dump?", api_key=_OWNER_KEY) == "ROUTE:a C++ crash dump?"
        assert _post_chat(port, "get a second opinion on sqlite vs postgres", api_key=_OWNER_KEY) == "CONSULT:sqlite vs postgres"
        assert _post_chat(port, "ask several local models to compare REST vs GraphQL", api_key=_OWNER_KEY) == "ENSEMBLE:compare REST vs GraphQL"
        assert _post_chat(port, "animate a cute pixel cat", api_key=_OWNER_KEY) == "![gif](a cute pixel cat)"


def test_http_run_and_run_window_followups_enforce_permission_gates(monkeypatch):
    seen = {"run": [], "window": []}
    monkeypatch.setattr(
        ts.code_runner,
        "run_code",
        lambda code, **kw: seen["run"].append((code, kw.get("language"))) or {
            "ok": True, "returncode": 0, "stdout": "hello sonder\n", "stderr": "",
        },
    )
    monkeypatch.setattr(ts.code_runner, "format_result", lambda _r: "hello sonder")
    monkeypatch.setattr(
        ts,
        "_do_run_window_from_messages",
        lambda timeout, **kw: seen["window"].append(timeout) or "opened window",
    )
    monkeypatch.setattr(
        ts._legacy_runtime(),
        "answer_with_history",
        lambda *a, **k: pytest.fail("run follow-up must not fall through to LLM"),
    )

    def _msgs(turn: str):
        return [
            {"role": "user", "content": "write a python script"},
            {"role": "assistant", "content": _PYTHON_REPLY},
            {"role": "user", "content": turn},
        ]

    with _http_server(monkeypatch, api_key=_OWNER_KEY) as port:
        pm.set_mode(pm.PLAN)
        assert "refused /run:" in _post_chat(port, "run it and show me the output", api_key=_OWNER_KEY, messages=_msgs("run it and show me the output"))
        assert "refused /runwindow:" in _post_chat(port, "run that in a new window", api_key=_OWNER_KEY, messages=_msgs("run that in a new window"))
        assert seen == {"run": [], "window": []}

        pm.set_mode(pm.AUTO)
        assert "[ran OK]" in _post_chat(port, "run it and show me the output", api_key=_OWNER_KEY, messages=_msgs("run it and show me the output"))
        assert "[ran OK]" in _post_chat(port, "try running the code you just wrote", api_key=_OWNER_KEY, messages=_msgs("try running the code you just wrote"))
        assert _post_chat(port, "run that in a new window", api_key=_OWNER_KEY, messages=_msgs("run that in a new window")) == "opened window"
        assert len(seen["run"]) == 2
        assert len(seen["window"]) == 1


def test_http_natural_command_bridge_enforces_security_boundaries():
    # Non-developer users never trigger host/control natural commands.
    for prompt in (
        "git status",
        "what os are you running on?",
        "what gpu do i have?",
        "set persona to security",
        "show me your stats",
    ):
        assert ts._handle_natural_command(prompt, context=_USER_CTX) is None, prompt

    # Destructive, file-mutating, and lifecycle commands remain explicit-slash-only.
    local_ctx = {"authorized": True, "mode": "local-open", "api_key": False, "account": None}
    for prompt in (
        "delete the file notes.txt",
        "write to file scratch.txt hello",
        "new session",
        "quit",
        "generate an image of a lighthouse",
    ):
        assert ts._handle_natural_command(prompt, context=local_ctx) is None, prompt


def test_anti_hijack_prompts_reach_chat_even_with_active_last_iid(monkeypatch):
    seen_prompts: list[str] = []
    monkeypatch.setattr(
        ts._legacy_runtime(),
        "answer_with_history",
        lambda prompt, *a, **k: seen_prompts.append(prompt) or f"CHAT_OK:{prompt}",
    )
    # Seed a state with an active last_iid so feedback classifier runs.
    with _http_server(monkeypatch, api_key=_OWNER_KEY) as port:
        state = ts._http_conversation_state(
            {"authorized": True, "mode": "api-key", "api_key": True, "account": None},
            "hijack-check",
        )
        state.last_iid = 42

        reply1 = _post_chat(
            port,
            "ELI5 how public key cryptography works",
            api_key=_OWNER_KEY,
            session="hijack-check",
        )
        reply2 = _post_chat(
            port,
            "forecast revenue growth for Q4",
            api_key=_OWNER_KEY,
            session="hijack-check",
        )
        assert reply1.startswith("CHAT_OK:ELI5")
        assert reply2.startswith("CHAT_OK:forecast")
        assert len(seen_prompts) == 2
