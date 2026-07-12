import json
import subprocess
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

import trilobite_launcher


class FakeController:
    def __init__(self):
        self.actions = []

    def status(self):
        return {
            "ok": True, "launcher": "ready", "server_running": False,
            "server_host": "0.0.0.0", "server_port": 11435,
            "last_action": "", "last_action_ts": 0, "last_error": "",
        }

    def action(self, action, context_size="8192"):
        self.actions.append((action, context_size))
        return {**self.status(), "ok": True, "server_running": action != "stop", "message": action}


@pytest.fixture
def launcher_server():
    token = "a" * 32
    controller = FakeController()
    server = trilobite_launcher.LauncherServer(
        ("127.0.0.1", 0), trilobite_launcher.LauncherHandler,
        controller=controller, token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], token, controller
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(url, token="", method="GET", body=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=3) as response:
        return response.status, json.loads(response.read())


def test_launcher_requires_authentication(launcher_server):
    base, token, _ = launcher_server
    with pytest.raises(urllib.error.HTTPError) as error:
        request(base + "/v1/launcher/status")
    assert error.value.code == 401
    status, payload = request(base + "/v1/launcher/status", token)
    assert status == 200 and payload["launcher"] == "ready"


def test_launcher_exposes_only_bounded_actions(launcher_server):
    base, token, controller = launcher_server
    status, payload = request(
        base + "/v1/launcher/start", token, "POST", {"context_size": "32k"},
    )
    assert status == 200 and payload["server_running"]
    assert controller.actions == [("start", "32k")]
    with pytest.raises(urllib.error.HTTPError) as error:
        request(base + "/v1/launcher/run", token, "POST", {"command": "whoami"})
    assert error.value.code == 404
    assert controller.actions == [("start", "32k")]


def test_launcher_returns_structured_service_failure(launcher_server, monkeypatch):
    base, token, controller = launcher_server
    monkeypatch.setattr(
        controller,
        "action",
        lambda *args, **kwargs: {
            **controller.status(),
            "ok": False,
            "message": "launcher command timed out",
            "last_error": "launcher command timed out",
        },
    )

    with pytest.raises(urllib.error.HTTPError) as error:
        request(base + "/v1/launcher/start", token, "POST", {})

    assert error.value.code == 503
    payload = json.loads(error.value.read())
    assert payload["ok"] is False
    assert payload["last_error"] == "launcher command timed out"


def test_launcher_rejects_invalid_or_oversized_inputs(launcher_server):
    base, token, controller = launcher_server
    for value in ("$(bad)", "1mk", "0", "1.1", "1000001", "1.1m"):
        with pytest.raises(urllib.error.HTTPError) as error:
            request(
                base + "/v1/launcher/start",
                token,
                "POST",
                {"context_size": value},
            )
        assert error.value.code == 400
    assert controller.actions == []


@pytest.mark.parametrize("value", ["1", "8192", "32k", "256.5k", "1m"])
def test_launcher_accepts_bounded_context_sizes(value):
    assert trilobite_launcher.normalize_context_size(value) == value


def test_lan_binding_requires_strong_token():
    with pytest.raises(ValueError, match="at least 24"):
        trilobite_launcher.validate_configuration("0.0.0.0", "short")
    trilobite_launcher.validate_configuration("0.0.0.0", "x" * 24)
    trilobite_launcher.validate_configuration("127.0.0.1", "")


def test_controller_constructs_fixed_headless_command(monkeypatch, tmp_path):
    (tmp_path / "trilobite_headless.py").write_text("# fixture", encoding="utf-8")
    seen = {}
    reachable = iter([False, True, True])
    monkeypatch.setattr(
        trilobite_launcher,
        "_reachable",
        lambda *args, **kwargs: next(reachable),
    )
    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="started", stderr="")
    monkeypatch.setattr(trilobite_launcher.subprocess, "run", fake_run)
    controller = trilobite_launcher.LauncherController(
        tmp_path, python="python-test", server_host="0.0.0.0", server_port=11435,
    )
    payload = controller.action("start", "8192")
    assert seen["command"] == [
        "python-test", str(tmp_path / "trilobite_headless.py"), "start",
        "--host", "0.0.0.0", "--port", "11435", "--context-size", "8192",
    ]
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["timeout"] <= trilobite_launcher.ACTION_TIMEOUT_SECONDS
    assert seen["kwargs"]["env"]["TRILOBITE_HOST"] == "0.0.0.0"
    assert seen["kwargs"]["env"]["TRILOBITE_PORT"] == "11435"
    assert payload["command"][:3] == ["python-test", "trilobite_headless.py", "start"]
    assert payload["ok"] is True


def test_stop_fails_when_server_remains_reachable(monkeypatch, tmp_path):
    controller = trilobite_launcher.LauncherController(tmp_path)
    monkeypatch.setattr(
        controller,
        "_run",
        lambda *args, **kwargs: (0, "stop requested", ["python", "headless", "stop"]),
    )
    monkeypatch.setattr(controller, "_wait_for_state", lambda *args: False)
    monkeypatch.setattr(trilobite_launcher, "_reachable", lambda *args, **kwargs: True)

    payload = controller.action("stop")

    assert payload["ok"] is False
    assert payload["server_running"] is True
    assert "remained reachable" in payload["last_error"]


def test_restart_waits_for_down_transition_before_start(monkeypatch, tmp_path):
    controller = trilobite_launcher.LauncherController(tmp_path)
    calls = []
    transitions = []

    def fake_run(action, context_size, timeout):
        calls.append(action)
        return 0, "%s complete" % action, ["python", "headless", action]

    def fake_wait(running, deadline):
        transitions.append(running)
        return True

    monkeypatch.setattr(controller, "_run", fake_run)
    monkeypatch.setattr(controller, "_wait_for_state", fake_wait)
    monkeypatch.setattr(trilobite_launcher, "_reachable", lambda *args, **kwargs: True)

    payload = controller.action("restart", "32k")

    assert payload["ok"] is True
    assert calls == ["stop", "start"]
    assert transitions == [False, True]
    assert [command[-1] for command in payload["commands"]] == ["stop", "start"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (subprocess.TimeoutExpired(["python", "headless"], 40), "timed out"),
        (OSError("runtime missing"), "could not start"),
    ],
)
def test_launcher_command_failures_are_structured(
    monkeypatch,
    tmp_path,
    error,
    expected,
):
    controller = trilobite_launcher.LauncherController(tmp_path)
    monkeypatch.setattr(
        trilobite_launcher.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(trilobite_launcher, "_reachable", lambda *args, **kwargs: False)

    payload = controller.action("start")

    assert payload["ok"] is False
    assert expected in payload["message"]
    assert expected in payload["last_error"]
