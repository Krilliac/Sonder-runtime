import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

import sonder_launcher


class FakeProcess:
    def __init__(self, *, output=b"started", returncode=0, wait_error=None):
        self.pid = 424242
        self.stdin = FakeStdin()
        self.stdout = io.BytesIO(output)
        self.returncode = None
        self._final_returncode = returncode
        self._wait_error = wait_error

    def wait(self, timeout=None):
        if self._wait_error is not None:
            error, self._wait_error = self._wait_error, None
            raise error
        self.returncode = self._final_returncode
        return self.returncode

    def send_signal(self, value):
        return None


class FakeStdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, value):
        assert not self.closed
        self.data.extend(value)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class FakeController:
    def __init__(self):
        self.actions = []
        self.idempotency_keys = []
        self.operations = {}

    def status(self):
        return {
            "ok": True, "launcher": "ready", "server_running": False,
            "server_host": "0.0.0.0", "server_port": 11435,
            "last_action": "", "last_action_ts": 0, "last_error": "",
            "server_state": "stopped", "active_operation": None,
        }

    def submit(self, action, context_size="8192", idempotency_key=""):
        self.actions.append((action, context_size))
        self.idempotency_keys.append(idempotency_key)
        operation = {
            "id": "%032x" % (len(self.operations) + 1),
            "action": action,
            "context_size": context_size,
            "phase": "queued",
            "created_ts": time.time(),
            "started_ts": None,
            "updated_ts": time.time(),
            "finished_ts": None,
            "message": "",
            "last_error": "",
            "command": [],
            "commands": [],
        }
        self.operations[operation["id"]] = operation
        return operation, True

    def operation(self, operation_id):
        return self.operations.get(operation_id)

    def operation_payload(self, operation):
        return {
            **self.status(),
            "operation_id": operation["id"],
            "operation_phase": operation["phase"],
            "operation": operation,
            "message": operation["message"],
            "command": operation["command"],
            "commands": operation["commands"],
        }


@pytest.fixture
def launcher_server():
    token = "a" * 32
    controller = FakeController()
    server = sonder_launcher.LauncherServer(
        ("127.0.0.1", 0), sonder_launcher.LauncherHandler,
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


def request(url, token="", method="GET", body=None, extra_headers=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    headers.update(extra_headers or {})
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
    assert status == 202 and payload["operation_phase"] == "queued"
    assert payload["accepted"] is True
    assert controller.actions == [("start", "32k")]
    status, polled = request(
        base + "/v1/launcher/operations/" + payload["operation_id"], token,
    )
    assert status == 200 and polled["operation"]["action"] == "start"
    with pytest.raises(urllib.error.HTTPError) as error:
        request(
            base + "/v1/launcher/operations/extra/" + payload["operation_id"],
            token,
        )
    assert error.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as error:
        request(base + "/v1/launcher/run", token, "POST", {"command": "whoami"})
    assert error.value.code == 404
    assert controller.actions == [("start", "32k")]


def test_launcher_returns_structured_queue_failure(launcher_server, monkeypatch):
    base, token, controller = launcher_server
    monkeypatch.setattr(
        controller,
        "submit",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ledger unavailable")),
    )

    with pytest.raises(urllib.error.HTTPError) as error:
        request(base + "/v1/launcher/start", token, "POST", {})

    assert error.value.code == 503
    payload = json.loads(error.value.read())
    assert payload["ok"] is False
    assert "ledger unavailable" in payload["error"]


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


def test_launcher_rejects_unbounded_body_fields_and_bad_idempotency_key(
    launcher_server,
):
    base, token, controller = launcher_server
    with pytest.raises(urllib.error.HTTPError) as error:
        request(
            base + "/v1/launcher/start",
            token,
            "POST",
            {"context_size": "8k", "command": "whoami"},
        )
    assert error.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as error:
        request(
            base + "/v1/launcher/start",
            token,
            "POST",
            {"context_size": "8k"},
            {"Idempotency-Key": "bad key"},
        )
    assert error.value.code == 400
    assert controller.actions == []


def test_launcher_accepts_and_forwards_idempotency_key(launcher_server):
    base, token, controller = launcher_server
    status, _ = request(
        base + "/v1/launcher/start",
        token,
        "POST",
        {"context_size": "8k"},
        {"Idempotency-Key": "mobile-request-1234"},
    )
    assert status == 202
    assert controller.idempotency_keys == ["mobile-request-1234"]


def test_launcher_rejects_unsafe_http_body_framing(launcher_server):
    base, token, controller = launcher_server

    def raw(data, headers):
        request_object = urllib.request.Request(
            base + "/v1/launcher/start",
            data=data,
            headers={"Authorization": "Bearer " + token, **headers},
            method="POST",
        )
        return urllib.request.urlopen(request_object, timeout=3)

    with pytest.raises(urllib.error.HTTPError) as error:
        raw(b"{}", {})
    assert error.value.code == 415
    with pytest.raises(urllib.error.HTTPError) as error:
        raw(
            b"{}",
            {"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        )
    assert error.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as error:
        raw(
            b"x" * (sonder_launcher.MAX_BODY + 1),
            {"Content-Type": "application/json"},
        )
    assert error.value.code == 413
    assert controller.actions == []


def test_launcher_rejects_incomplete_request_body(launcher_server):
    base, token, controller = launcher_server
    parsed = urllib.parse.urlsplit(base)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=3) as client:
        client.sendall(
            (
                "POST /v1/launcher/start HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Authorization: Bearer %s\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 10\r\n"
                "Connection: close\r\n\r\n{}"
            ).encode("ascii")
            % token.encode("ascii")
        )
        client.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
    assert b" 400 " in response.split(b"\r\n", 1)[0]
    assert b"request body is incomplete" in response
    assert controller.actions == []


@pytest.mark.parametrize("value", ["1", "8192", "32k", "256.5k", "1m"])
def test_launcher_accepts_bounded_context_sizes(value):
    assert sonder_launcher.normalize_context_size(value) == value


def test_lan_binding_requires_strong_token():
    with pytest.raises(ValueError, match="at least 24"):
        sonder_launcher.validate_configuration("0.0.0.0", "short")
    sonder_launcher.validate_configuration("0.0.0.0", "x" * 24)
    sonder_launcher.validate_configuration("127.0.0.1", "")
    sonder_launcher.validate_configuration("::1", "")


def test_main_reports_controller_initialization_failure(monkeypatch):
    monkeypatch.setattr(
        sonder_launcher,
        "LauncherController",
        lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("bad db")),
    )
    assert sonder_launcher.main(["--host", "127.0.0.1"]) == 2


def make_controller(tmp_path, **kwargs):
    return sonder_launcher.LauncherController(
        tmp_path,
        db_path=tmp_path / "sonder-launcher.sqlite3",
        health_token_path=tmp_path / "sonder-launcher-health.token",
        start_timeout=kwargs.pop("start_timeout", 2),
        stop_timeout=kwargs.pop("stop_timeout", 2),
        **kwargs,
    )


def test_controller_constructs_fixed_headless_command(monkeypatch, tmp_path):
    (tmp_path / "sonder_headless.py").write_text("# fixture", encoding="utf-8")
    seen = {}
    states = iter(["stopped", "healthy", "healthy"])
    def fake_popen(command, **kwargs):
        process = FakeProcess()
        seen.update(command=command, kwargs=kwargs, process=process)
        return process
    monkeypatch.setattr(sonder_launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        sonder_launcher, "_process_start_identity", lambda pid: "test-process"
    )
    controller = make_controller(
        tmp_path, python="python-test", server_host="0.0.0.0", server_port=11435,
    )
    def persist_before_release(*args):
        assert bytes(seen["process"].stdin.data) == b""
        seen["persisted"] = True
    monkeypatch.setattr(controller, "_persist_control_started", persist_before_release)
    monkeypatch.setattr(controller, "_server_state", lambda: next(states))
    payload = controller.action("start", "8192")
    assert seen["command"] == [
        "python-test", str(tmp_path / "sonder_headless.py"), "start",
        "--host", "0.0.0.0", "--port", "11435", "--context-size", "8192",
    ]
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["stdin"] is subprocess.PIPE
    assert seen["kwargs"]["stdout"] is subprocess.PIPE
    assert seen["kwargs"]["stderr"] is subprocess.STDOUT
    assert seen["kwargs"]["env"]["SONDER_HOST"] == "0.0.0.0"
    assert seen["kwargs"]["env"]["SONDER_PORT"] == "11435"
    assert seen["kwargs"]["env"][sonder_launcher.CONTROL_GATE_ENV] == "1"
    assert (
        seen["kwargs"]["env"][sonder_launcher.sonder_health.ROLE_ENV]
        == sonder_launcher.sonder_health.MANAGED_ROLE
    )
    assert bytes(seen["process"].stdin.data) == b"\x01"
    assert seen["process"].stdin.closed is True
    assert seen["persisted"] is True
    assert (
        seen["kwargs"]["env"][sonder_launcher.sonder_health.TOKEN_ENV]
        == controller.health_token
    )
    assert payload["command"][:3] == ["python-test", "sonder_headless.py", "start"]
    assert payload["ok"] is True


def test_stop_fails_when_server_remains_reachable(monkeypatch, tmp_path):
    controller = make_controller(tmp_path)
    monkeypatch.setattr(
        controller,
        "_run",
        lambda *args, **kwargs: (0, "stop requested", ["python", "headless", "stop"]),
    )
    monkeypatch.setattr(controller, "_wait_for_state", lambda *args: False)
    monkeypatch.setattr(controller, "_server_state", lambda: "healthy")

    payload = controller.action("stop")

    assert payload["ok"] is False
    assert payload["server_running"] is True
    assert "remained healthy" in payload["last_error"]


def test_restart_waits_for_down_transition_before_start(monkeypatch, tmp_path):
    controller = make_controller(tmp_path)
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
    monkeypatch.setattr(controller, "_server_state", lambda: "healthy")

    payload = controller.action("restart", "32k")

    assert payload["ok"] is True
    assert calls == ["stop", "start"]
    assert transitions == [False, True]
    assert [command[-1] for command in payload["commands"]] == ["stop", "start"]


def test_launcher_command_start_failure_is_structured(monkeypatch, tmp_path):
    controller = make_controller(tmp_path)
    monkeypatch.setattr(
        sonder_launcher.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("runtime missing")),
    )
    monkeypatch.setattr(controller, "_server_state", lambda: "stopped")

    payload = controller.action("start")

    assert payload["ok"] is False
    assert "could not start" in payload["message"]
    assert "could not start" in payload["last_error"]


def test_launcher_command_timeout_is_structured(monkeypatch, tmp_path):
    controller = make_controller(tmp_path)
    process = FakeProcess(
        returncode=-9,
        wait_error=subprocess.TimeoutExpired(["python", "headless"], 2),
    )
    monkeypatch.setattr(sonder_launcher.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(
        sonder_launcher, "_process_start_identity", lambda pid: "test-process"
    )
    monkeypatch.setattr(controller, "_terminate_control_tree", lambda *a, **k: True)
    monkeypatch.setattr(controller, "_server_state", lambda: "stopped")

    payload = controller.action("start")

    assert payload["ok"] is False
    assert "timed out" in payload["message"]
    assert "timed out" in payload["last_error"]


def _successful_result(action):
    return {
        "ok": True,
        "message": "%s complete" % action,
        "last_error": "",
        "commands": [["python", "sonder_headless.py", action]],
    }


def test_async_operation_is_persisted_and_pollable(monkeypatch, tmp_path):
    controller = make_controller(tmp_path)
    monkeypatch.setattr(
        controller,
        "action",
        lambda action, context_size, timeout=None: _successful_result(action),
    )

    queued, created = controller.submit(
        "start", "32k", "mobile-request-0001"
    )
    finished = controller.wait_operation(queued["id"], timeout=2)

    assert created is True
    assert queued["phase"] == "queued"
    assert finished["phase"] == "succeeded"
    assert finished["commands"][-1][-1] == "start"
    reopened = make_controller(tmp_path)
    assert reopened.operation(queued["id"])["phase"] == "succeeded"


def test_idempotent_replay_and_single_active_operation(monkeypatch, tmp_path):
    controller = make_controller(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocked(action, context_size, timeout=None):
        entered.set()
        assert release.wait(2)
        return _successful_result(action)

    monkeypatch.setattr(controller, "action", blocked)
    first, created = controller.submit("start", "8k", "mobile-request-0002")
    assert created is True and entered.wait(1)

    replay, created = controller.submit("start", "8k", "mobile-request-0002")
    assert created is False and replay["id"] == first["id"]
    with pytest.raises(sonder_launcher.LauncherConflictError, match="different"):
        controller.submit("start", "16k", "mobile-request-0002")
    with pytest.raises(sonder_launcher.LauncherConflictError, match="already active"):
        controller.submit("stop", "8k")

    release.set()
    assert controller.wait_operation(first["id"], 2)["phase"] == "succeeded"


def test_cross_controller_lock_does_not_steal_from_live_local_owner(
    monkeypatch,
    tmp_path,
):
    controller = make_controller(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocked(action, context_size, timeout=None):
        entered.set()
        assert release.wait(2)
        return _successful_result(action)

    monkeypatch.setattr(controller, "action", blocked)
    operation, _ = controller.submit("start")
    assert entered.wait(1)
    with sqlite3.connect(controller.db_path) as connection:
        connection.execute(
            "UPDATE sonder_launcher_operation_lock SET lease_until=0 WHERE id=1"
        )
    second = make_controller(tmp_path)

    assert second.status()["active_operation"]["id"] == operation["id"]
    with pytest.raises(sonder_launcher.LauncherConflictError):
        second.submit("stop")

    release.set()
    assert controller.wait_operation(operation["id"], 2)["phase"] == "succeeded"


def test_dead_owner_is_interrupted_and_stale_worker_cannot_overwrite(
    monkeypatch,
    tmp_path,
):
    controller = make_controller(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocked(action, context_size, timeout=None):
        entered.set()
        assert release.wait(2)
        return _successful_result(action)

    monkeypatch.setattr(controller, "action", blocked)
    operation, _ = controller.submit("start")
    assert entered.wait(1)
    with controller._threads_lock:
        worker = controller._threads[operation["id"]]
    with sqlite3.connect(controller.db_path) as connection:
        connection.execute(
            """
            UPDATE sonder_launcher_operation_lock
            SET owner_pid=99999999, lease_until=0 WHERE id=1
            """
        )

    recovered = make_controller(tmp_path)
    assert recovered.operation(operation["id"])["phase"] == "interrupted"
    assert recovered.status()["active_operation"] is None
    release.set()
    worker.join(timeout=2)
    assert recovered.operation(operation["id"])["phase"] == "interrupted"


def test_operation_output_and_history_are_bounded(monkeypatch, tmp_path):
    controller = make_controller(tmp_path, retention=2)
    monkeypatch.setattr(
        controller,
        "action",
        lambda action, context_size, timeout=None: {
            **_successful_result(action),
            "message": "x" * (sonder_launcher.MAX_OPERATION_OUTPUT + 500),
        },
    )
    ids = []
    for index in range(3):
        operation, _ = controller.submit(
            "start", idempotency_key="retention-key-%04d" % index
        )
        ids.append(operation["id"])
        finished = controller.wait_operation(operation["id"], 2)
        assert len(finished["message"]) == sonder_launcher.MAX_OPERATION_OUTPUT
        assert finished["message"].startswith("[output truncated]\n")

    with sqlite3.connect(controller.db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM sonder_launcher_operations"
        ).fetchone()[0]
    assert count == 2
    assert controller.operation(ids[0]) is None
    assert controller.operation(ids[-1])["phase"] == "succeeded"


def test_health_token_is_persistent_private_and_passed_without_proxy(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv(sonder_launcher.sonder_health.TOKEN_ENV, raising=False)
    controller = make_controller(tmp_path, server_port=25435)
    assert len(controller.health_token) >= 32
    assert os.stat(controller.health_token_path).st_mode & 0o077 == 0
    assert make_controller(tmp_path, server_port=25435).health_token == controller.health_token

    seen = {}

    class Response:
        status = 200

        def read(self, size):
            return json.dumps(
                sonder_launcher.sonder_health.response_payload(
                    controller.health_token,
                    seen["nonce"],
                    controller.server_port,
                    pid=123,
                )
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            headers = {key.lower(): value for key, value in request.headers.items()}
            seen["nonce"] = headers[
                sonder_launcher.sonder_health.NONCE_HEADER.lower()
            ]
            return Response()

    def build_opener(handler):
        seen["proxies"] = handler.proxies
        return Opener()

    monkeypatch.setattr(sonder_launcher, "_reachable", lambda *args: True)
    monkeypatch.setattr(sonder_launcher.urllib.request, "build_opener", build_opener)

    assert controller._server_state() == "healthy"
    assert seen["proxies"] == {}
    header_values = {key.lower(): value for key, value in seen["request"].headers.items()}
    assert sonder_launcher.sonder_health.NONCE_HEADER.lower() in header_values
    assert "x-sonder-launcher-health-token" not in header_values
    assert controller.health_token not in header_values.values()


def test_launcher_status_reports_only_verified_managed_role(monkeypatch, tmp_path):
    controller = make_controller(tmp_path)
    monkeypatch.setattr(controller, "_server_state", lambda: "healthy")
    payload = controller.status()
    assert payload["server_running"] is True
    assert payload["server_role"] == sonder_launcher.sonder_health.MANAGED_ROLE


def test_foreign_listener_blocks_all_mutating_commands(monkeypatch, tmp_path):
    controller = make_controller(tmp_path)
    monkeypatch.setattr(controller, "_server_state", lambda: "foreign_listener")
    monkeypatch.setattr(
        controller,
        "_run",
        lambda *args, **kwargs: pytest.fail("foreign listener must block commands"),
    )

    for action in ("start", "stop", "restart"):
        payload = controller.action(action)
        assert payload["ok"] is False
        assert payload["server_state"] == "foreign_listener"
        assert "unverified listener" in payload["last_error"]


def test_windows_pid_probe_never_uses_os_kill(monkeypatch):
    monkeypatch.setattr(sonder_launcher.os, "name", "nt")
    monkeypatch.setattr(
        sonder_launcher.os,
        "kill",
        lambda *args: pytest.fail("os.kill must not be called on Windows"),
    )
    assert sonder_launcher._pid_alive(99999999) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_timeout_terminates_control_grandchild_and_bounds_output(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "sonder_headless.py").write_text(
        """
import signal
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print("x" * 100000, flush=True)
print("child=%s" % child.pid, flush=True)
time.sleep(30)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(sonder_launcher, "CONTROL_TERMINATE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(sonder_launcher, "CONTROL_KILL_GRACE_SECONDS", 1.0)
    controller = make_controller(tmp_path, python=sys.executable)

    code, output, _ = controller._run("start", timeout=0.2)

    assert code == 124
    assert len(output) <= sonder_launcher.MAX_OPERATION_OUTPUT
    assert output.startswith("[output truncated]\n")
    assert "timed out" in output
    child_pid = int(
        next(line for line in output.splitlines() if line.startswith("child=")).split(
            "=", 1
        )[1]
    )
    fields = sonder_launcher._linux_process_fields(child_pid)
    assert not fields or fields["state"] == "Z"


@pytest.mark.skipif(os.name == "nt", reason="POSIX detached-process regression")
def test_successful_control_command_does_not_kill_detached_child(tmp_path):
    (tmp_path / "sonder_headless.py").write_text(
        """
import subprocess
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
""",
        encoding="utf-8",
    )
    controller = make_controller(tmp_path, python=sys.executable)

    code, output, _ = controller._run("start", timeout=2)
    child_pid = int(output.strip())
    fields = sonder_launcher._linux_process_fields(child_pid)
    try:
        assert code == 0
        assert fields and fields["state"] != "Z"
    finally:
        if fields:
            try:
                os.killpg(fields["group"], sonder_launcher.signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX failed-tree regression")
def test_nonzero_control_exit_terminates_delayed_grandchild(monkeypatch, tmp_path):
    (tmp_path / "sonder_headless.py").write_text(
        """
import subprocess
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
raise SystemExit(7)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(sonder_launcher, "CONTROL_TERMINATE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(sonder_launcher, "CONTROL_KILL_GRACE_SECONDS", 1.0)
    controller = make_controller(tmp_path, python=sys.executable)

    code, output, _ = controller._run("start", timeout=2)
    child_pid = int(output.strip())
    fields = sonder_launcher._linux_process_fields(child_pid)

    assert code == 7
    assert not fields or fields["state"] == "Z"


@pytest.mark.skipif(os.name == "nt", reason="POSIX persisted-tree regression")
def test_stale_recovery_terminates_persisted_control_tree(tmp_path):
    script = tmp_path / "owned_tree.py"
    script.write_text(
        """
import signal
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
time.sleep(30)
""",
        encoding="utf-8",
    )
    parent = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_pid = int(parent.stdout.readline().strip())
    identity = sonder_launcher._process_start_identity(parent.pid)
    controller = make_controller(tmp_path)
    operation_id = "d" * 32
    owner_id = "dead-owner"
    now = time.time()
    with sqlite3.connect(controller.db_path) as connection:
        connection.execute(
            """
            INSERT INTO sonder_launcher_operations(
                id,action,context_size,phase,created_ts,updated_ts,owner_id,
                owner_pid,owner_host,lease_until,hard_deadline,control_pid,
                control_identity,control_group_id,control_platform,
                control_started_ts
            ) VALUES(?,?,?,'running',?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                operation_id, "start", "8192", now, now, owner_id,
                99999999, socket.gethostname(), 0, 0, parent.pid, identity,
                parent.pid, "posix-session", now,
            ),
        )
        connection.execute(
            """
            UPDATE sonder_launcher_operation_lock SET operation_id=?, owner_id=?,
                owner_pid=99999999, owner_host=?, lease_until=0 WHERE id=1
            """,
            (operation_id, owner_id, socket.gethostname()),
        )
    try:
        recovered = make_controller(tmp_path)
        parent.wait(timeout=3)
        assert recovered.operation(operation_id)["phase"] == "interrupted"
        assert not (
            sonder_launcher._linux_process_fields(child_pid)
            and sonder_launcher._linux_process_fields(child_pid)["state"] != "Z"
        )
        assert recovered.status()["active_operation"] is None
    finally:
        try:
            os.killpg(parent.pid, sonder_launcher.signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_stale_recovery_retains_lock_when_tree_cannot_be_proven_gone(
    monkeypatch,
    tmp_path,
):
    controller = make_controller(tmp_path)
    operation_id = "c" * 32
    owner_id = "unreconciled-owner"
    now = time.time()
    with sqlite3.connect(controller.db_path) as connection:
        connection.execute(
            """
            INSERT INTO sonder_launcher_operations(
                id,action,context_size,phase,created_ts,updated_ts,owner_id,
                owner_pid,owner_host,lease_until,hard_deadline,control_pid,
                control_identity,control_group_id,control_platform
            ) VALUES(?,?,?,'running',?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                operation_id, "start", "8192", now, now, owner_id,
                99999999, socket.gethostname(), 0, 0, 43210,
                "unknown-process", 43210, "posix-session",
            ),
        )
        connection.execute(
            """
            UPDATE sonder_launcher_operation_lock SET operation_id=?, owner_id=?,
                owner_pid=99999999, owner_host=?, lease_until=0 WHERE id=1
            """,
            (operation_id, owner_id, socket.gethostname()),
        )
    monkeypatch.setattr(controller, "_terminate_control_tree", lambda *args: False)

    controller.recover_interrupted()

    with sqlite3.connect(controller.db_path) as connection:
        phase, last_error = connection.execute(
            "SELECT phase,last_error FROM sonder_launcher_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        lock_id = connection.execute(
            "SELECT operation_id FROM sonder_launcher_operation_lock WHERE id=1"
        ).fetchone()[0]
    assert phase == "running"
    assert "could not be proven stopped" in last_error
    assert lock_id == operation_id


def test_remote_owner_never_signals_coincident_local_process_group(
    monkeypatch,
    tmp_path,
):
    controller = make_controller(tmp_path)
    monkeypatch.setattr(sonder_launcher, "_process_matches", lambda *args: True)
    monkeypatch.setattr(
        sonder_launcher.os,
        "killpg",
        lambda *args: pytest.fail("remote process metadata must never be signalled"),
    )
    operation = {
        "control_pid": 43210,
        "control_identity": "remote-boot:identity",
        "control_group_id": 43210,
        "control_platform": "posix-session",
        "control_exit_code": None,
        "owner_host": "different-host.example",
    }

    assert controller._terminate_control_tree(operation) is False


def test_control_identity_is_persisted_before_worker_waits(monkeypatch, tmp_path):
    (tmp_path / "sonder_headless.py").write_text(
        "import time\nprint('working', flush=True)\ntime.sleep(0.5)\n",
        encoding="utf-8",
    )
    controller = make_controller(tmp_path, python=sys.executable)
    states = iter(["stopped", "healthy"])
    monkeypatch.setattr(controller, "_server_state", lambda: next(states))
    monkeypatch.setattr(controller, "_wait_for_state", lambda *args: True)

    operation, _ = controller.submit("start")
    row = None
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with sqlite3.connect(controller.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM sonder_launcher_operations WHERE id=?",
                (operation["id"],),
            ).fetchone()
        if row and row["control_pid"]:
            break
        time.sleep(0.01)

    assert row["phase"] == "running"
    assert row["control_pid"] == row["control_group_id"]
    assert row["control_identity"]
    assert row["control_platform"] in {"posix-session", "windows-process-group"}
    assert row["hard_deadline"] > row["created_ts"]
    assert controller.wait_operation(operation["id"], 2)["phase"] == "succeeded"


@pytest.mark.skipif(os.name == "nt", reason="POSIX detached recovery regression")
def test_stale_recovery_preserves_proven_successful_detached_child(tmp_path):
    script = tmp_path / "successful_detach.py"
    script.write_text(
        """
import subprocess
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
""",
        encoding="utf-8",
    )
    parent = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    identity = sonder_launcher._process_start_identity(parent.pid)
    child_pid = int(parent.stdout.readline().strip())
    parent.wait(timeout=2)
    controller = make_controller(tmp_path)
    operation_id = "e" * 32
    owner_id = "dead-after-success"
    now = time.time()
    with sqlite3.connect(controller.db_path) as connection:
        connection.execute(
            """
            INSERT INTO sonder_launcher_operations(
                id,action,context_size,phase,created_ts,updated_ts,owner_id,
                owner_pid,owner_host,lease_until,hard_deadline,control_pid,
                control_identity,control_group_id,control_platform,
                control_started_ts,control_finished_ts,control_exit_code
            ) VALUES(?,?,?,'running',?,?,?,?,?,?,?,?,?,?,?,?,?,0)
            """,
            (
                operation_id, "start", "8192", now, now, owner_id,
                99999999, socket.gethostname(), 0, 0, parent.pid, identity,
                parent.pid, "posix-session", now, now,
            ),
        )
        connection.execute(
            """
            UPDATE sonder_launcher_operation_lock SET operation_id=?, owner_id=?,
                owner_pid=99999999, owner_host=?, lease_until=0 WHERE id=1
            """,
            (operation_id, owner_id, socket.gethostname()),
        )
    child_fields = sonder_launcher._linux_process_fields(child_pid)
    try:
        recovered = make_controller(tmp_path)
        current_fields = sonder_launcher._linux_process_fields(child_pid)
        assert recovered.operation(operation_id)["phase"] == "interrupted"
        assert current_fields and current_fields["state"] != "Z"
        assert recovered.status()["active_operation"] is None
    finally:
        if child_fields:
            try:
                os.killpg(child_fields["group"], sonder_launcher.signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_expired_hard_deadline_recovers_even_with_live_worker(monkeypatch, tmp_path):
    controller = make_controller(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocked(action, context_size, timeout=None):
        entered.set()
        assert release.wait(2)
        return _successful_result(action)

    monkeypatch.setattr(controller, "action", blocked)
    operation, _ = controller.submit("start")
    assert entered.wait(1)
    with sqlite3.connect(controller.db_path) as connection:
        connection.execute(
            "UPDATE sonder_launcher_operations SET hard_deadline=0.5 WHERE id=?",
            (operation["id"],),
        )

    controller.recover_interrupted()
    assert controller.operation(operation["id"])["phase"] == "interrupted"
    release.set()
    assert controller.wait_operation(operation["id"], 2)["phase"] == "interrupted"


def test_forced_normal_finalization_failure_uses_emergency_transaction(
    monkeypatch,
    tmp_path,
):
    controller = make_controller(tmp_path)
    monkeypatch.setattr(
        controller,
        "action",
        lambda action, context_size, timeout=None: _successful_result(action),
    )
    calls = []

    def fail_finalize(*args, **kwargs):
        calls.append(1)
        raise sqlite3.OperationalError("forced finalization failure")

    monkeypatch.setattr(controller, "_finish_operation", fail_finalize)
    operation, _ = controller.submit("start")
    finished = controller.wait_operation(operation["id"], 2)

    assert len(calls) == len(sonder_launcher.FINALIZE_RETRY_DELAYS)
    assert finished["phase"] == "succeeded"
    assert controller.status()["active_operation"] is None


def test_total_finalization_failure_is_recovered_after_worker_exits(
    monkeypatch,
    tmp_path,
):
    controller = make_controller(tmp_path)
    monkeypatch.setattr(
        controller,
        "action",
        lambda action, context_size, timeout=None: _successful_result(action),
    )
    monkeypatch.setattr(
        controller,
        "_finish_operation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("normal finalizer unavailable")
        ),
    )
    monkeypatch.setattr(
        controller,
        "_emergency_finish_operation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("emergency finalizer unavailable")
        ),
    )

    operation, _ = controller.submit("start")
    finished = controller.wait_operation(operation["id"], 2)

    assert finished["phase"] == "interrupted"
    assert controller.status()["active_operation"] is None
