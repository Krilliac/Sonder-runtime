"""Unit and integration tests for /v1/sonder/node GPU pool parsing and doctor diagnostics."""
from __future__ import annotations

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import sonder_doctor
from sonder_runtime.adapters.inference import exchange_transport, gpu_residency
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    ENV_PRIVATE_WORKERS,
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.bootstrap.doctor_checks import summarize_inference_node_diagnostics
from sonder_runtime.domain.inference_profiles import (
    NODE_ABSENT,
    NODE_ERROR,
    NODE_OK,
    NODE_SCHEMA,
    format_node_gpu_pool_summary,
    parse_inference_node_document,
    parse_node_gpu_device,
    parse_node_gpu_pool,
    summarize_node_gpu_pool,
)

PRIMARY = "http://127.0.0.1:18437"
NODE1 = "https://192.0.2.20:8443"
NODE1_TOKEN = "n" * 32
PRIMARY_KEY = "p" * 32
BOUND_ENV = {"SONDER_MODEL_BACKEND": "sonder-inference", "SONDER_EMBEDDING_PROVIDER": "ollama"}
GIB = 1024 * 1024 * 1024


def _health_doc() -> dict[str, Any]:
    return {
        "status": "ready",
        "api_version": 1,
        "version": "0.2.0",
        "synthetic": False,
        "models": [{"id": "qwen3-80b", "backend": "llamacpp", "default": True}],
    }


def _hybrid_node_doc() -> dict[str, Any]:
    """Representative Node1 /v1/sonder/node payload (discrete RX 9060 XT + Radeon 780M UMA)."""
    return {
        "schema": NODE_SCHEMA,
        "node_id": "node1",
        "instance_id": "inst-node1",
        "platform": "windows-x86_64",
        "version": "0.2.0",
        "api_version": 1,
        "synthetic": False,
        "host_memory": {
            "total_physical_bytes": 64 * GIB,
            "available_physical_bytes": 48 * GIB,
            "available_commit_bytes": 52 * GIB,
            "source": "GlobalMemoryStatusEx",
        },
        "gpus": [
            {
                "id": "gpu:0",
                "name": "AMD Radeon RX 9060 XT",
                "vendor": "amd",
                "vendor_id": 4098,
                "device_id": 29999,
                "dedicated_total_mib": 16304,
                "dedicated_free_mib": 15900,
                "shared_total_mib": 32592,
                "effective_vram_mib": 16304,
                "effective_free_vram_mib": 15900,
                "is_integrated_uma": False,
                "backend_hint": "vulkan",
                "source": "dxgi",
            },
            {
                "id": "gpu:1",
                "name": "AMD Radeon 780M Graphics",
                "vendor": "amd",
                "vendor_id": 4098,
                "device_id": 5500,
                "dedicated_total_mib": 512,
                "dedicated_free_mib": 480,
                "shared_total_mib": 32592,
                "effective_vram_mib": 32592,
                "effective_free_vram_mib": 28220,
                "is_integrated_uma": True,
                "backend_hint": "vulkan",
                "source": "dxgi",
            },
        ],
        "combined_gpu_vram_mib": 48896,
        "gpu_pool": {
            "discrete_vram_mib": 16304,
            "integrated_uma_vram_mib": 32592,
            "combined_gpu_vram_mib": 48896,
            "discrete_free_vram_mib": 15900,
            "integrated_uma_free_vram_mib": 28220,
            "combined_free_gpu_vram_mib": 44120,
            "discrete_gpu_count": 1,
            "integrated_uma_gpu_count": 1,
            "hybrid_vulkan_pool_available": True,
            "recommended_devices": ["Vulkan0", "Vulkan1"],
            "llama_arg_device": "Vulkan0,Vulkan1",
        },
        "capabilities": {
            "ternary_gguf": True,
            "placement_planner": True,
            "coresidency_planner": True,
            "multi_gpu": True,
            "hybrid_gpu_spread": True,
            "uma_telemetry": True,
            "adaptive_speculation": True,
        },
    }


def _discrete_only_node_doc() -> dict[str, Any]:
    """Representative Main PC /v1/sonder/node payload (discrete RTX 5070 Ti 16 GiB)."""
    return {
        "schema": NODE_SCHEMA,
        "node_id": "main-pc",
        "platform": "windows-x86_64",
        "version": "0.2.0",
        "api_version": 1,
        "synthetic": False,
        "host_memory": {
            "total_physical_bytes": 32 * GIB,
            "available_physical_bytes": 20 * GIB,
            "source": "GlobalMemoryStatusEx",
        },
        "gpus": [
            {
                "id": "gpu:0",
                "name": "NVIDIA GeForce RTX 5070 Ti",
                "vendor": "nvidia",
                "dedicated_total_mib": 16303,
                "dedicated_free_mib": 14800,
                "effective_vram_mib": 16303,
                "effective_free_vram_mib": 14800,
                "is_integrated_uma": False,
                "backend_hint": "cuda",
                "source": "nvml",
            },
        ],
        "combined_gpu_vram_mib": 16303,
        "gpu_pool": {
            "discrete_vram_mib": 16303,
            "integrated_uma_vram_mib": 0,
            "combined_gpu_vram_mib": 16303,
            "discrete_free_vram_mib": 14800,
            "integrated_uma_free_vram_mib": 0,
            "combined_free_gpu_vram_mib": 14800,
            "discrete_gpu_count": 1,
            "integrated_uma_gpu_count": 0,
            "hybrid_vulkan_pool_available": False,
            "recommended_devices": [],
            "llama_arg_device": None,
        },
        "capabilities": {
            "multi_gpu": True,
            "hybrid_gpu_spread": True,
            "uma_telemetry": True,
        },
    }


# ---------------------------------------------------------------------------
# 1. Pure domain parser & formatter
# ---------------------------------------------------------------------------


def test_parse_hybrid_discrete_and_uma_node_document():
    snap = parse_inference_node_document(_hybrid_node_doc(), base_url=NODE1)
    assert snap is not None
    assert snap.state == NODE_OK
    assert snap.schema == NODE_SCHEMA
    assert snap.node_id == "node1"
    assert snap.api_version == 1
    assert snap.combined_gpu_vram_mib == 48896
    assert snap.hybrid_gpu_spread is True
    assert snap.uma_telemetry is True
    assert snap.multi_gpu is True
    assert len(snap.gpus) == 2
    assert snap.gpus[0].is_integrated_uma is False
    assert snap.gpus[0].effective_vram_mib == 16304
    assert snap.gpus[1].is_integrated_uma is True
    assert snap.gpus[1].shared_total_mib == 32592
    assert snap.gpus[1].effective_vram_mib == 32592

    pool = snap.gpu_pool
    assert pool is not None
    assert pool.discrete_vram_mib == 16304
    assert pool.integrated_uma_vram_mib == 32592
    assert pool.combined_gpu_vram_mib == 48896
    assert pool.combined_free_gpu_vram_mib == 44120
    assert pool.discrete_gpu_count == 1
    assert pool.integrated_uma_gpu_count == 1
    assert pool.hybrid_vulkan_pool_available is True
    assert pool.recommended_devices == ("Vulkan0", "Vulkan1")
    assert pool.llama_arg_device == "Vulkan0,Vulkan1"

    line = format_node_gpu_pool_summary(snap)
    assert "48896 MiB combined" in line
    assert "1 discrete: 16304 MiB" in line
    assert "1 UMA: 32592 MiB" in line
    assert "free 44120 MiB" in line
    assert "hybrid_gpu_spread=available (Vulkan0,Vulkan1)" in line


def test_parse_pre_pr97_node_synthesizes_gpu_pool_with_host_ram_uma_cap():
    """Older /v1/sonder/node without gpu_pool derives pool from gpus[] and caps UMA at host RAM."""
    doc = {
        "schema": NODE_SCHEMA,
        "api_version": 1,
        "host_memory": {
            "total_physical_bytes": 24 * GIB,
            "available_physical_bytes": 16 * GIB,
        },
        "gpus": [
            {
                "id": "gpu:0",
                "name": "Discrete GPU",
                "vendor": "amd",
                "dedicated_total_mib": 12000,
                "dedicated_free_mib": 10000,
                "effective_vram_mib": 12000,
                "effective_free_vram_mib": 10000,
                "is_integrated_uma": False,
            },
            {
                "id": "gpu:1",
                "name": "Integrated UMA GPU",
                "vendor": "amd",
                "shared_total_mib": 32768,
                "effective_vram_mib": 32768,
                "effective_free_vram_mib": 28000,
                "is_integrated_uma": True,
            },
        ],
        "capabilities": {"multi_gpu": True, "uma_telemetry": True},
    }
    snap = parse_inference_node_document(doc, base_url=PRIMARY)
    assert snap is not None
    assert snap.hybrid_gpu_spread is False
    assert snap.gpu_pool is not None
    # Integrated UMA 32768 MiB is capped at 24 GiB (24576 MiB) total and 16 GiB (16384 MiB) free host RAM.
    assert snap.gpu_pool.discrete_vram_mib == 12000
    assert snap.gpu_pool.integrated_uma_vram_mib == 24 * 1024
    assert snap.gpu_pool.integrated_uma_free_vram_mib == 16 * 1024
    assert snap.gpu_pool.combined_gpu_vram_mib == 12000 + 24 * 1024
    assert snap.combined_gpu_vram_mib == 12000 + 24 * 1024
    # Never fabricates hybrid Vulkan spread when the server did not attest it.
    assert snap.gpu_pool.hybrid_vulkan_pool_available is False
    assert snap.gpu_pool.recommended_devices == ()


def test_parse_cpu_only_and_discrete_only_node_summaries():
    discrete_snap = parse_inference_node_document(_discrete_only_node_doc(), base_url=PRIMARY)
    assert discrete_snap is not None
    line = format_node_gpu_pool_summary(discrete_snap)
    assert "16303 MiB combined" in line
    assert "1 discrete: 16303 MiB" in line
    assert "UMA" not in line
    assert "hybrid_gpu_spread=supported" in line

    cpu_doc = {
        "schema": NODE_SCHEMA,
        "api_version": 1,
        "gpus": [],
        "combined_gpu_vram_mib": 0,
        "gpu_pool": {
            "discrete_vram_mib": 0,
            "integrated_uma_vram_mib": 0,
            "combined_gpu_vram_mib": 0,
            "discrete_gpu_count": 0,
            "integrated_uma_gpu_count": 0,
            "hybrid_vulkan_pool_available": False,
        },
        "capabilities": {"hybrid_gpu_spread": False},
    }
    cpu_snap = parse_inference_node_document(cpu_doc, base_url=PRIMARY)
    assert cpu_snap is not None
    cpu_line = format_node_gpu_pool_summary(cpu_snap)
    assert "0 MiB (no GPU)" in cpu_line
    assert "hybrid_gpu_spread=no" in cpu_line


def test_parser_rejects_malformed_adversarial_and_non_node_documents():
    # Health / identity documents must never parse as node snapshots.
    assert parse_inference_node_document(_health_doc()) is None
    assert parse_inference_node_document({"schema": "sonder.inference.identity/1", "api_version": 1}) is None
    assert parse_inference_node_document({"schema": "evil/2", "api_version": 1}) is None
    assert parse_inference_node_document({"schema": NODE_SCHEMA, "api_version": 2}) is None
    assert parse_inference_node_document(["not", "an", "object"]) is None

    # Bools, negatives, NaN, free > total, and control/shell injection are sanitized or rejected.
    adversarial = {
        "schema": NODE_SCHEMA,
        "api_version": 1,
        "node_id": "node1\r\nFAKE: header",
        "gpus": [
            {
                "id": "gpu:0",
                "name": "RX\x1b[31m9060\nXT",
                "vendor": "AMD",
                "dedicated_total_mib": 16000,
                "dedicated_free_mib": 99999,  # > total -> clamped
                "effective_vram_mib": True,  # bool rejected
                "effective_free_vram_mib": -5,  # negative rejected
                "is_integrated_uma": False,
            }
        ],
        "combined_gpu_vram_mib": False,  # bool rejected
        "gpu_pool": {
            "discrete_vram_mib": 16000,
            "discrete_free_vram_mib": 25000,  # > total -> clamped to 16000
            "integrated_uma_vram_mib": -100,  # invalid -> fallback 0
            "hybrid_vulkan_pool_available": True,
            "recommended_devices": ["Vulkan0", "bad;rm -rf /", "Vulkan1", "../etc"],
            "llama_arg_device": "Vulkan0;whoami",
        },
        "capabilities": {
            "hybrid_gpu_spread": True,
            "bad key!": True,
            "not_a_bool": "yes",
        },
    }
    snap = parse_inference_node_document(adversarial, base_url=PRIMARY)
    assert snap is not None
    assert "\n" not in (snap.node_id or "") and "\r" not in (snap.node_id or "")
    assert "\x1b" not in snap.gpus[0].name and "\n" not in snap.gpus[0].name
    assert snap.gpus[0].effective_vram_mib is None
    assert snap.gpus[0].effective_free_vram_mib is None
    assert snap.gpus[0].dedicated_free_mib == 16000
    assert snap.gpu_pool is not None
    assert snap.gpu_pool.discrete_free_vram_mib == 16000
    assert snap.gpu_pool.integrated_uma_vram_mib == 0
    assert snap.gpu_pool.recommended_devices == ("Vulkan0", "Vulkan1")
    # Malicious llama_arg_device rejected; safely rebuilt from validated recommended_devices.
    assert snap.gpu_pool.llama_arg_device == "Vulkan0,Vulkan1"
    assert dict(snap.capabilities) == {"hybrid_gpu_spread": True}
    assert parse_node_gpu_device({}) is None
    assert parse_node_gpu_pool(None) is None
    assert summarize_node_gpu_pool(()).combined_gpu_vram_mib == 0


# ---------------------------------------------------------------------------
# 2. Transport security, TLS trust_scope isolation, caching, and multi-node
# ---------------------------------------------------------------------------


def test_remote_endpoint_policy_blocks_unconsented_or_plaintext_node_probe():
    called = [False]

    def fail_if_called(*_args):
        called[0] = True
        raise AssertionError("policy refusal must happen before any network I/O")

    for env in (
        {"SONDER_INFERENCE_BASE_URL": "http://192.0.2.20:11437"},
        {"SONDER_INFERENCE_BASE_URL": "https://192.0.2.20:8443", "SONDER_INFERENCE_API_KEY": PRIMARY_KEY},
        {"SONDER_INFERENCE_BASE_URL": "https://192.0.2.20:8443", "SONDER_ALLOW_REMOTE_INFERENCE": "1"},
        {"SONDER_INFERENCE_BASE_URL": "http://127.0.0.2:11437"},
    ):
        snap = gpu_residency.inspect_inference_node(env=env, get_transport=fail_if_called)
        assert snap.state == NODE_ERROR
        assert PRIMARY_KEY not in snap.detail
        assert called[0] is False


def test_multi_node_inspection_enforces_worker_tls_ca_and_token_isolation(tmp_path):
    ca_file = tmp_path / "node1-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8")

    observed: list[tuple[str, dict[str, str], str]] = []

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        assert timeout <= 6.0
        observed.append((url, dict(headers), getattr(exchange_transport._TRUST, "ca_bundle", "")))
        if url.startswith(NODE1):
            return 200, json.dumps(_hybrid_node_doc()).encode("utf-8")
        return 200, json.dumps(_discrete_only_node_doc()).encode("utf-8")

    env = {
        "SONDER_INFERENCE_BASE_URL": PRIMARY,
        "SONDER_INFERENCE_API_KEY": PRIMARY_KEY,
        "SONDER_ALLOW_REMOTE_INFERENCE": "1",
        ENV_PRIVATE_WORKERS: json.dumps([
            {"url": NODE1, "ca_bundle": str(ca_file), "token_env": "SONDER_NODE1_TEST_TOKEN"},
        ]),
        "SONDER_NODE1_TEST_TOKEN": NODE1_TOKEN,
    }
    report = gpu_residency.inspect_inference_nodes(env=env, get_transport=fake_get)

    # TLS trust_scope was active strictly for the worker request and restored afterwards.
    assert getattr(exchange_transport._TRUST, "ca_bundle", "") == ""
    assert len(observed) == 2
    primary_url, primary_headers, primary_ca = observed[0]
    worker_url, worker_headers, worker_ca = observed[1]
    assert primary_url == PRIMARY + "/v1/sonder/node"
    assert primary_headers.get("Authorization") == "Bearer " + PRIMARY_KEY
    assert primary_ca == ""
    assert worker_url == NODE1 + "/v1/sonder/node"
    assert worker_headers.get("Authorization") == "Bearer " + NODE1_TOKEN
    assert worker_ca == str(ca_file)

    assert report["total_nodes"] == 2
    assert report["ready_nodes"] == 2
    assert report["discrete_vram_mib"] == 16303 + 16304
    assert report["integrated_uma_vram_mib"] == 32592
    assert report["combined_gpu_vram_mib"] == 16303 + 48896
    assert report["combined_free_gpu_vram_mib"] == 14800 + 44120
    assert report["hybrid_gpu_spread_nodes"] == [NODE1]
    assert "Vulkan0,Vulkan1" in report["summary"]
    # Secrets never appear anywhere in the content-free diagnostic report.
    serialized = json.dumps(report)
    assert PRIMARY_KEY not in serialized
    assert NODE1_TOKEN not in serialized

    verdict = summarize_inference_node_diagnostics(report)
    assert verdict["status"] == "ok"


def test_worker_missing_token_or_tls_error_fails_closed_without_breaking_primary(tmp_path):
    ca_file = tmp_path / "node1-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8")

    # Case A: worker token too short -> refused locally before I/O, primary still succeeds.
    called_urls: list[str] = []

    def fake_get_primary(url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        called_urls.append(url)
        return 200, json.dumps(_discrete_only_node_doc()).encode("utf-8")

    env_short_token = {
        "SONDER_INFERENCE_BASE_URL": PRIMARY,
        "SONDER_ALLOW_REMOTE_INFERENCE": "1",
        ENV_PRIVATE_WORKERS: json.dumps([
            {"url": NODE1, "ca_bundle": str(ca_file), "token_env": "SONDER_NODE1_TEST_TOKEN"},
        ]),
        "SONDER_NODE1_TEST_TOKEN": "tok-tiny-99",
    }
    report_a = gpu_residency.inspect_inference_nodes(env=env_short_token, get_transport=fake_get_primary)
    assert called_urls == [PRIMARY + "/v1/sonder/node"]
    assert report_a["ready_nodes"] == 1
    assert report_a["workers"][0]["state"] == NODE_ERROR
    assert "tok-tiny-99" not in json.dumps(report_a)
    assert summarize_inference_node_diagnostics(report_a)["status"] == "warn"

    # Case B: TLS cert verification failure on worker -> clean NODE_ERROR, trust_scope restored, no secrets.
    def fake_get_tls_fail(url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        if url.startswith(NODE1):
            assert getattr(exchange_transport._TRUST, "ca_bundle", "") == str(ca_file)
            raise ssl.SSLCertVerificationError("certificate verify failed")
        return 200, json.dumps(_discrete_only_node_doc()).encode("utf-8")

    env_tls = dict(env_short_token, SONDER_NODE1_TEST_TOKEN=NODE1_TOKEN)
    report_b = gpu_residency.inspect_inference_nodes(env=env_tls, get_transport=fake_get_tls_fail)
    assert getattr(exchange_transport._TRUST, "ca_bundle", "") == ""
    assert report_b["ready_nodes"] == 1
    assert report_b["workers"][0]["state"] == NODE_ERROR
    assert "TLS certificate verification failed" in report_b["workers"][0]["detail"]
    assert NODE1_TOKEN not in json.dumps(report_b)


def test_node_probe_handles_http_404_401_oversized_and_ttl_cache():
    clock = [100.0]
    cache = gpu_residency.NodeSnapshotCache(monotonic=lambda: clock[0])
    calls = [0]

    def fake_get(_url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        calls[0] += 1
        return 200, json.dumps(_hybrid_node_doc()).encode("utf-8")

    cfg = SonderInferenceConfig(base_url=PRIMARY)
    first = gpu_residency.inspect_inference_node(settings=cfg, get_transport=fake_get, ttl=60.0, cache=cache)
    second = gpu_residency.inspect_inference_node(settings=cfg, get_transport=fake_get, ttl=60.0, cache=cache)
    assert first.state == NODE_OK and second.combined_gpu_vram_mib == 48896
    assert calls[0] == 1
    assert cache.cached(PRIMARY) is not None

    # Past TTL or explicit refresh=True triggers a fresh fetch.
    clock[0] = 200.0
    gpu_residency.inspect_inference_node(settings=cfg, get_transport=fake_get, ttl=60.0, cache=cache)
    assert calls[0] == 2
    gpu_residency.inspect_inference_node(settings=cfg, get_transport=fake_get, ttl=60.0, refresh=True, cache=cache)
    assert calls[0] == 3

    # HTTP 404 -> NODE_ABSENT; HTTP 401 -> NODE_ERROR; oversized body -> NODE_ERROR.
    absent = gpu_residency.inspect_inference_node(
        settings=cfg, get_transport=lambda *_a: (404, b'{"error":"not found"}'),
    )
    assert absent.state == NODE_ABSENT
    auth_err = gpu_residency.inspect_inference_node(
        settings=cfg, get_transport=lambda *_a: (401, b'{"error":"secret body leak"}'),
    )
    assert auth_err.state == NODE_ERROR
    assert "secret body leak" not in auth_err.detail
    oversized = gpu_residency.inspect_inference_node(
        settings=cfg, get_transport=lambda *_a: (200, b"x" * (gpu_residency.NODE_BODY_LIMIT + 1)),
    )
    assert oversized.state == NODE_ERROR
    assert "exceeded" in oversized.detail


# ---------------------------------------------------------------------------
# 3. Real loopback HTTP server + sonder_doctor end-to-end integration
# ---------------------------------------------------------------------------


def test_doctor_surfaces_node_gpu_pool_over_real_loopback_http():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            if self.path == "/v1/sonder/health":
                raw = json.dumps(_health_doc()).encode("utf-8")
                self.send_response(200)
            elif self.path == "/v1/sonder/node":
                raw = json.dumps(_hybrid_node_doc()).encode("utf-8")
                self.send_response(200)
            elif self.path == "/v1/models":
                raw = json.dumps({"object": "list", "data": []}).encode("utf-8")
                self.send_response(200)
            else:
                raw = b'{"error":"not found"}'
                self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base_url = "http://127.0.0.1:%d" % httpd.server_address[1]
        env = dict(BOUND_ENV, SONDER_INFERENCE_BASE_URL=base_url)
        gpu_residency.shared_node_cache().clear()

        check = sonder_doctor._check_sonder_inference(env=env)
        assert check["status"] == "ok"
        assert "gpu pool: 48896 MiB combined" in check["detail"]
        assert "1 discrete: 16304 MiB" in check["detail"]
        assert "1 UMA: 32592 MiB" in check["detail"]
        assert "hybrid_gpu_spread=available (Vulkan0,Vulkan1)" in check["detail"]

        # Shared cache is populated after the real loopback doctor check (zero-I/O read works).
        cached = gpu_residency.cached_inference_node(base_url)
        assert cached is not None and cached.combined_gpu_vram_mib == 48896

        report = sonder_doctor.sonder_inference_node_report(env=env)
        assert report["verdict"]["status"] == "ok"
        assert report["combined_gpu_vram_mib"] == 48896
        assert report["hybrid_gpu_spread_nodes"] == [base_url]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_doctor_stays_unchanged_when_node_endpoint_returns_404_or_redirect():
    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            if self.path == "/v1/sonder/node":
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base_url = "http://127.0.0.1:%d" % httpd.server_address[1]
        # Redirect must NEVER be followed (no SSRF).
        snap = gpu_residency.inspect_inference_node(
            settings=SonderInferenceConfig(base_url=base_url),
            cache=gpu_residency.NodeSnapshotCache(),
        )
        assert snap.state == NODE_ERROR
        assert "169.254.169.254" not in snap.detail
    finally:
        httpd.shutdown()
        httpd.server_close()

    # Older server returning 404 on /v1/sonder/node keeps doctor check clean & unchanged.
    def older_get(url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        if url.endswith("/v1/sonder/health"):
            return 200, json.dumps(_health_doc()).encode("utf-8")
        return 404, b'{"error":"not found"}'

    gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url=PRIMARY),
        get_transport=older_get,
        transport=lambda *_a: {},
    )
    check = sonder_doctor._check_sonder_inference(env=BOUND_ENV, gateway=gw)
    assert check == {"status": "ok", "detail": f"{PRIMARY}: ready: 1 model(s)"}
    report = sonder_doctor.sonder_inference_node_report(env=BOUND_ENV, gateway=gw)
    assert report["verdict"]["status"] == "skipped"


def test_doctor_multi_node_check_combines_primary_and_private_worker_gpu_pools(tmp_path):
    ca_file = tmp_path / "node1-ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8")

    def fake_get(url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        if url.endswith("/v1/sonder/health"):
            return 200, json.dumps(_health_doc()).encode("utf-8")
        if url == PRIMARY + "/v1/sonder/node":
            return 200, json.dumps(_discrete_only_node_doc()).encode("utf-8")
        if url == NODE1 + "/v1/sonder/node":
            return 200, json.dumps(_hybrid_node_doc()).encode("utf-8")
        return 404, b'{"error":"not found"}'

    env = {
        **BOUND_ENV,
        "SONDER_INFERENCE_BASE_URL": PRIMARY,
        "SONDER_ALLOW_REMOTE_INFERENCE": "1",
        ENV_PRIVATE_WORKERS: json.dumps([
            {"url": NODE1, "ca_bundle": str(ca_file), "token_env": "SONDER_NODE1_TEST_TOKEN"},
        ]),
        "SONDER_NODE1_TEST_TOKEN": NODE1_TOKEN,
    }
    gw = SonderInferenceGateway(env=env, get_transport=fake_get, transport=lambda *_a: {})
    check = sonder_doctor._check_sonder_inference(env=env, gateway=gw)
    assert check["status"] == "ok"
    assert "gpu pool: 16303 MiB combined" in check["detail"]
    assert f"worker {NODE1} gpu pool: 48896 MiB combined" in check["detail"]
    assert "hybrid_gpu_spread=available (Vulkan0,Vulkan1)" in check["detail"]
    assert NODE1_TOKEN not in check["detail"]
