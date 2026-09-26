"""Doctor and preflight coverage for a bound Sonder Inference provider.

The gateway under test is the production SonderInferenceGateway; only its
GET seam is scripted, except for the CLI tests, which probe a real refused
loopback port and a real in-process loopback HTTP server.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import sonder_doctor
from sonder_runtime.__main__ import main
from sonder_runtime.adapters import preflight as preflight_adapter
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.application.ports.preflight import CheckResult

BOUND = {"SONDER_MODEL_BACKEND": "sonder-inference", "SONDER_EMBEDDING_PROVIDER": "ollama"}


def _health(status="ready", *, api_version=1, synthetic=False):
    return {"status": status, "api_version": api_version, "version": "0.1.0",
            "synthetic": synthetic, "models": [{"id": "m", "backend": "mock", "default": True}]}


def _gateway(*, health=None, status=200, refused=False):
    def get(url, headers, timeout):
        assert timeout <= 2.0
        if refused:
            raise ConnectionRefusedError(111, "refused")
        if "/identity" in url:
            return 200, json.dumps({"schema": "sonder.inference.identity/1", "model": "m",
                                    "synthetic": False, "backend_identity": None,
                                    "reason": "not measurable"}).encode()
        return status, json.dumps(health or _health()).encode()

    def post(*_args):
        raise AssertionError("doctor must never generate")

    return SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:18437"),
        transport=post, get_transport=get,
    )


def test_not_configured_is_skipped():
    for env in ({}, {"SONDER_MODEL_BACKEND": "ollama"}):
        result = sonder_doctor._check_sonder_inference(env=env)
        assert result["status"] == "skipped" and "not configured" in result["detail"]
        assert sonder_doctor._check_sonder_inference_scope(env=env)["status"] == "skipped"


def test_ready_is_ok():
    result = sonder_doctor._check_sonder_inference(env=BOUND, gateway=_gateway())
    assert result == {"status": "ok", "detail": "http://127.0.0.1:18437: ready: 1 model(s)"}


def test_synthetic_mock_backend_warns():
    result = sonder_doctor._check_sonder_inference(
        env=BOUND, gateway=_gateway(health=_health(synthetic=True)),
    )
    assert result["status"] == "warn" and "MOCK backend" in result["detail"]


def test_unreachable_with_fallback_warns_and_without_fails():
    fallback = dict(BOUND, SONDER_INFERENCE_FALLBACK="ollama")
    warn = sonder_doctor._check_sonder_inference(env=fallback, gateway=_gateway(refused=True))
    assert warn["status"] == "warn" and "fall back to ollama" in warn["detail"]
    fail = sonder_doctor._check_sonder_inference(env=BOUND, gateway=_gateway(refused=True))
    assert fail["status"] == "fail"
    assert "sonder-infer serve" in fail["detail"]
    assert "SONDER_INFERENCE_FALLBACK=ollama" in fail["detail"]


def test_starting_server_without_fallback_fails():
    result = sonder_doctor._check_sonder_inference(
        env=BOUND, gateway=_gateway(health=_health("starting"), status=503),
    )
    assert result["status"] == "fail" and "starting" in result["detail"]


def test_api_version_mismatch_fails_even_with_fallback():
    fallback = dict(BOUND, SONDER_INFERENCE_FALLBACK="ollama")
    result = sonder_doctor._check_sonder_inference(
        env=fallback, gateway=_gateway(health=_health(api_version=2)),
    )
    assert result["status"] == "fail" and "incompatible" in result["detail"]


def test_invalid_bindings_fail_loudly():
    result = sonder_doctor._check_sonder_inference(env={"SONDER_MODEL_BACKEND": "sonder"})
    assert result["status"] == "fail" and "sonder-inference" in result["detail"]


def test_scope_warning_names_the_surfaces_bindings_do_not_reach():
    result = sonder_doctor._check_sonder_inference_scope(env=BOUND)
    assert result["status"] == "warn"
    for surface in ("REPL", "MCP", "autopilot", "fleet"):
        assert surface in result["detail"]


def test_default_and_bound_registries_include_the_checks():
    assert [name for name, _ in sonder_doctor.default_checks()][-2:] == [
        "sonder_inference", "sonder_inference_scope",
    ]
    assert [name for name, _ in sonder_doctor.sonder_inference_checks({})] == [
        "sonder_inference", "sonder_inference_scope",
    ]


# -- CLI --------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(path))
    return path


def _only_inference_checks(monkeypatch):
    monkeypatch.setattr(sonder_doctor, "default_checks", lambda: [
        ("sonder_inference", sonder_doctor._check_sonder_inference),
        ("sonder_inference_scope", sonder_doctor._check_sonder_inference_scope),
    ])


def _refused_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_cli_fails_for_bound_unreachable_inference_and_skip_flag_removes_it(
    home, monkeypatch, capsys,
):
    _only_inference_checks(monkeypatch)
    for key, value in BOUND.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:%d" % _refused_port())
    assert main(["doctor", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    statuses = {check["name"]: check["status"] for check in payload["checks"]}
    assert statuses == {"sonder_inference": "fail", "sonder_inference_scope": "warn"}

    assert main(["doctor", "--json", "--skip-inference"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"] == []


def test_cli_reports_a_real_ready_server_ok(home, monkeypatch, capsys):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            body = (_health() if self.path.startswith("/v1/sonder/health") else {
                "schema": "sonder.inference.identity/1", "model": "m", "synthetic": False,
                "backend_identity": None, "reason": "not measurable"})
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _only_inference_checks(monkeypatch)
        for key, value in BOUND.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:%d" % server.server_address[1])
        assert main(["doctor", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["checks"][0]["status"] == "ok"
    finally:
        server.shutdown()
        server.server_close()


# -- preflight ----------------------------------------------------------------------


def test_preflight_never_blocks_serve_because_of_inference(monkeypatch):
    for name, value in (
        ("_check_state_directories", lambda config: []),
        ("_check_disk_space", lambda config: CheckResult("disk_space", True, True, "ok")),
        ("_check_schema_versions", lambda config: []),
        ("_check_runtime_policy", lambda: CheckResult("runtime_policy", True, True, "ok")),
    ):
        monkeypatch.setattr(preflight_adapter, name, value)
    for key, value in BOUND.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:%d" % _refused_port())
    report = preflight_adapter.run_preflight(object(), check_ollama=False)
    inference = next(check for check in report.checks if check.name == "sonder_inference")
    assert inference.ok is False and inference.required is False
    assert report.ok is True and report.degraded is True

    monkeypatch.setenv("SONDER_MODEL_BACKEND", "ollama")
    monkeypatch.delenv("SONDER_EMBEDDING_PROVIDER")
    report = preflight_adapter.run_preflight(object(), check_ollama=False)
    assert all(check.name != "sonder_inference" for check in report.checks)


# -- verdicts follow what a request would meet ---------------------------------------


FALLBACK = dict(BOUND, SONDER_INFERENCE_FALLBACK="ollama")


@pytest.mark.parametrize("extra,needle", [
    ({"SONDER_INFERENCE_BASE_URL": "http://gpu.example:11437"}, "not loopback"),
    ({"SONDER_INFERENCE_TIMEOUT_SECONDS": "abc"}, "SONDER_INFERENCE_TIMEOUT_SECONDS"),
    ({"SONDER_INFERENCE_TIER_MODELS": "sonder=x"}, "unknown tier"),
    ({"SONDER_INFERENCE_BASE_URL": "http://127.0.0.2:11437"}, "loopback alias"),
])
def test_failures_no_fallback_can_help_fail_even_with_the_fallback(extra, needle):
    env = dict(FALLBACK, **extra)
    result = sonder_doctor._check_sonder_inference(env=env)
    assert result["status"] == "fail", result
    assert needle in result["detail"]
    assert "fall back" not in result["detail"]
    assert not result["detail"].startswith("unconfigured endpoint")


def test_ready_file_api_mismatch_fails_even_with_the_fallback(tmp_path):
    ready = tmp_path / "ready.json"
    ready.write_text(json.dumps({"url": "http://127.0.0.1:18437", "api_version": 2}))
    env = dict(FALLBACK, SONDER_INFERENCE_READY_FILE=str(ready))
    result = sonder_doctor._check_sonder_inference(env=env)
    assert result["status"] == "fail" and "api_version 2" in result["detail"]
    assert "fall back" not in result["detail"]


def test_missing_ready_file_is_unreachable_and_the_fallback_warns(tmp_path):
    env = dict(FALLBACK, SONDER_INFERENCE_READY_FILE=str(tmp_path / "absent.json"))
    result = sonder_doctor._check_sonder_inference(env=env)
    assert result["status"] == "warn" and "fall back to ollama" in result["detail"]


@pytest.mark.parametrize("health,status,needle", [
    ({"error": {"code": "unauthorized", "message": "bad key"}}, 401, "SONDER_INFERENCE_API_KEY"),
    ({"error": {"code": "forbidden_host", "message": "host"}}, 403, "127.0.0.1, localhost"),
    (_health(api_version=2), 200, "incompatible"),
])
def test_credentials_host_and_api_problems_fail_even_with_the_fallback(health, status, needle):
    result = sonder_doctor._check_sonder_inference(
        env=FALLBACK, gateway=_gateway(health=health, status=status),
    )
    assert result["status"] == "fail" and needle in result["detail"]
    assert "fall back" not in result["detail"]
    assert "SONDER_INFERENCE_FALLBACK=ollama" not in result["detail"]


def test_overloaded_server_is_a_transient_warning():
    overloaded = {"error": {"code": "overloaded", "message": "busy"}, "sonder": {"api_version": 1}}
    for env in (BOUND, FALLBACK):
        result = sonder_doctor._check_sonder_inference(
            env=env, gateway=_gateway(health=overloaded, status=503),
        )
        assert result["status"] == "warn" and "connection limit" in result["detail"]
        assert "incompatible" not in result["detail"]


def _garbage_listener():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    stop = threading.Event()

    def loop():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            try:
                # Consume the request first so the close is orderly (unread
                # input would RST the socket and turn this into a reset).
                conn.settimeout(2)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
            except OSError:
                pass
            conn.close()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

    def close():
        stop.set()
        thread.join(2)
        listener.close()

    return listener.getsockname()[1], close


def test_a_non_http_peer_never_crashes_preflight_or_doctor(monkeypatch):
    port, close = _garbage_listener()
    try:
        for name, value in (
            ("_check_state_directories", lambda config: []),
            ("_check_disk_space", lambda config: CheckResult("disk_space", True, True, "ok")),
            ("_check_schema_versions", lambda config: []),
            ("_check_runtime_policy", lambda: CheckResult("runtime_policy", True, True, "ok")),
        ):
            monkeypatch.setattr(preflight_adapter, name, value)
        for key, value in BOUND.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:%d" % port)
        report = preflight_adapter.run_preflight(object(), check_ollama=False)
        inference = next(check for check in report.checks if check.name == "sonder_inference")
        assert inference.ok is False and inference.required is False
        assert "malformed HTTP" in inference.detail
        assert report.ok is True

        result = sonder_doctor._check_sonder_inference(env=dict(
            BOUND, SONDER_INFERENCE_BASE_URL="http://127.0.0.1:%d" % port,
        ))
        assert result["status"] == "fail" and "malformed HTTP" in result["detail"]
    finally:
        close()


def test_preflight_reports_a_check_that_raises_instead_of_blocking(monkeypatch):
    def explode():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(preflight_adapter, "_sonder_inference_result", explode)
    result = preflight_adapter._check_sonder_inference()
    assert result.ok is False and result.required is False
    assert "RuntimeError" in result.detail
