import json
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


def test_launcher_rejects_invalid_or_oversized_inputs(launcher_server):
    base, token, controller = launcher_server
    with pytest.raises(urllib.error.HTTPError) as error:
        request(base + "/v1/launcher/start", token, "POST", {"context_size": "$(bad)"})
    assert error.value.code == 400
    assert controller.actions == []


def test_lan_binding_requires_strong_token():
    with pytest.raises(ValueError, match="at least 24"):
        trilobite_launcher.validate_configuration("0.0.0.0", "short")
    trilobite_launcher.validate_configuration("0.0.0.0", "x" * 24)
    trilobite_launcher.validate_configuration("127.0.0.1", "")


def test_controller_constructs_fixed_headless_command(monkeypatch, tmp_path):
    (tmp_path / "trilobite_headless.py").write_text("# fixture", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(trilobite_launcher, "_reachable", lambda *args, **kwargs: False)
    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="started", stderr="")
    monkeypatch.setattr(trilobite_launcher.subprocess, "run", fake_run)
    monotonic = iter([0, 31])
    monkeypatch.setattr(trilobite_launcher.time, "monotonic", lambda: next(monotonic))
    controller = trilobite_launcher.LauncherController(
        tmp_path, python="python-test", server_host="0.0.0.0", server_port=11435,
    )
    payload = controller.action("start", "8192")
    assert seen["command"] == [
        "python-test", str(tmp_path / "trilobite_headless.py"), "start",
        "--host", "0.0.0.0", "--port", "11435", "--context-size", "8192",
    ]
    assert "shell" not in seen["kwargs"]
    assert payload["command"][:3] == ["python-test", "trilobite_headless.py", "start"]
