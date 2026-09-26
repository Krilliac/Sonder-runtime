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
