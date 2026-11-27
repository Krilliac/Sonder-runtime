"""FED-03 tests: per-node health gate (NAT-08 telemetry, effective-BW %, pp/tg deltas, degraded routing)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

import pytest

from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    ENV_NODE_HEALTH_BASELINES,
    ENV_PRIVATE_WORKERS,
    SonderInferenceGateway,
    parse_node_health_baselines,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.inference_profiles import (
    NODE_SCHEMA,
    NodeGgmlDeviceTelemetry,
    NodePcieTelemetry,
    NodeVulkanMemoryHeap,
    parse_health_gpus,
    parse_inference_node_document,
    parse_node_gpu_device,
)
from sonder_runtime.domain.routing.node_health_gate import (
    NODE_STATE_DEGRADED,
    NODE_STATE_HEALTHY,
    NODE_STATE_PROBATION,
    NODE_STATE_UNKNOWN,
    NodeHealthBaseline,
    NodeHealthGate,
    NodeHealthGatePolicy,
    compute_baseline_delta_pct,
    compute_effective_bandwidth_gbps,
    compute_effective_bandwidth_pct,
    evaluate_gpu_device_telemetry,
    known_gpu_spec_bandwidth_gbps,
)

PRIMARY = "http://127.0.0.1:11437"
WORKER = "https://192.0.2.2:8443/sonder-inference"
TOKEN = "t" * 32
GIB = 1024 * 1024 * 1024


def _nat08_gpu_json(
    *,
    name: str = "AMD Radeon RX 9060 XT",
    is_uma: bool = False,
    rebar: bool | None = True,
    current_gen: int | None = 4,
    max_gen: int | None = 4,
    current_width: int | None = 4,
    max_width: int | None = 4,
    aspm_state: str | None = "off",
    budget_bytes: int | None = 15 * GIB,
    usage_bytes: int | None = 6 * GIB,
    host_heap_usage_bytes: int | None = 128 * 1024 * 1024,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "gpu:0",
        "name": name,
        "vendor": "amd" if "AMD" in name else "nvidia",
        "dedicated_total_mib": 16304,
        "dedicated_free_mib": 10000,
        "shared_total_mib": 16384,
        "effective_vram_mib": 16304,
        "effective_free_vram_mib": 10000,
        "is_integrated_uma": is_uma,
        "backend_hint": "vulkan" if "AMD" in name else "cuda",
        "source": "dxgi",
        "pci_bus_id": "0000:01:00.0",
        "driver_name": "AMD proprietary driver" if "AMD" in name else "NVIDIA",
        "driver_build": "25.10.2 (32.0.21025.1024)",
        "shared_memory_bytes": 32768,
        "warp_size": 64 if "AMD" in name else 32,
        "matrix_cores": "KHR_coopmat" if "AMD" in name else "NV_coopmat2v",
        "pcie": {
            "current_gen": current_gen,
            "current_width": current_width,
            "max_gen": max_gen,
            "max_width": max_width,
            "aspm_state": aspm_state,
            "aspm_source": "windows_power_plan",
        },
        "vulkan_heaps": [
            {
                "heap_index": 0,
                "size_bytes": 16 * GIB,
                "budget_bytes": budget_bytes,
                "usage_bytes": usage_bytes,
                "device_local": True,
                "multi_instance": False,
                "host_visible": True,
                "host_coherent": True,
            },
            {
                "heap_index": 1,
                "size_bytes": 24 * GIB,
                "budget_bytes": 20 * GIB,
                "usage_bytes": host_heap_usage_bytes,
                "device_local": False,
                "multi_instance": False,
                "host_visible": True,
                "host_coherent": True,
            },
        ],
        "ggml_device": {
            "backend": "vulkan",
            "index": 0,
            "device_name": name,
            "driver_name": "AMD proprietary driver" if "AMD" in name else "NVIDIA",
            "uma": is_uma,
            "rebar": rebar,
            "fp16": "1",
            "bf16": True,
            "fp4": False,
            "warp_size": 64 if "AMD" in name else 32,
            "shared_memory_bytes": 32768,
            "int_dot": True,
            "matrix_cores": "KHR_coopmat" if "AMD" in name else "NV_coopmat2v",
        },
    }
    if rebar is not None:
        entry["rebar"] = rebar
    return entry


# -- 1. NAT-08 telemetry parsing in domain/inference_profiles.py -------------


def test_parse_node_gpu_device_reads_nat08_telemetry_fields() -> None:
    raw = _nat08_gpu_json()
    gpu = parse_node_gpu_device(raw)
    assert gpu is not None
    assert gpu.pci_bus_id == "0000:01:00.0"
    assert gpu.driver_name == "AMD proprietary driver"
    assert gpu.driver_build == "25.10.2 (32.0.21025.1024)"
    assert gpu.rebar is True
    assert gpu.shared_memory_bytes == 32768
    assert gpu.warp_size == 64
    assert gpu.matrix_cores == "KHR_coopmat"
    assert isinstance(gpu.pcie, NodePcieTelemetry)
    assert gpu.pcie.current_gen == 4
    assert gpu.pcie.max_gen == 4
    assert gpu.pcie.current_width == 4
    assert gpu.pcie.max_width == 4
    assert gpu.pcie.aspm_state == "off"
    assert len(gpu.vulkan_heaps) == 2
    assert isinstance(gpu.vulkan_heaps[0], NodeVulkanMemoryHeap)
    assert gpu.vulkan_heaps[0].device_local is True
    assert gpu.vulkan_heaps[0].budget_bytes == 15 * GIB
    assert gpu.vulkan_heaps[0].usage_bytes == 6 * GIB
    assert isinstance(gpu.ggml_device, NodeGgmlDeviceTelemetry)
    assert gpu.ggml_device.backend == "vulkan"
    assert gpu.ggml_device.rebar is True
    assert gpu.ggml_device.matrix_cores == "KHR_coopmat"

    as_dict = gpu.to_dict()
    assert as_dict["driver_build"] == "25.10.2 (32.0.21025.1024)"
    assert as_dict["rebar"] is True
    assert as_dict["pcie"]["current_width"] == 4
    assert len(as_dict["vulkan_heaps"]) == 2
    assert as_dict["ggml_device"]["backend"] == "vulkan"


def test_parse_node_gpu_device_omits_absent_nat08_fields_for_backward_compat() -> None:
    minimal = {
        "id": "gpu:0",
        "name": "NVIDIA GeForce RTX 5070 Ti",
        "vendor": "nvidia",
        "dedicated_total_mib": 16303,
        "dedicated_free_mib": 14000,
        "is_integrated_uma": False,
    }
    gpu = parse_node_gpu_device(minimal)
    assert gpu is not None
    assert gpu.rebar is None
    assert gpu.pcie is None
    assert gpu.vulkan_heaps == ()
    assert gpu.ggml_device is None
    as_dict = gpu.to_dict()
    assert "rebar" not in as_dict
    assert "pcie" not in as_dict
    assert "vulkan_heaps" not in as_dict
    assert "ggml_device" not in as_dict


def test_parse_health_gpus_and_node_document_preserve_nat08_telemetry() -> None:
    health_doc = {
        "status": "ready",
        "api_version": 1,
        "gpus": [_nat08_gpu_json()],
    }
    gpus = parse_health_gpus(health_doc)
    assert len(gpus) == 1
    assert gpus[0].rebar is True
    assert gpus[0].vulkan_heaps[0].budget_bytes == 15 * GIB

    node_doc = {
        "schema": NODE_SCHEMA,
        "api_version": 1,
        "node_id": "node1",
        "gpus": [_nat08_gpu_json(rebar=None)],
    }
    snap = parse_inference_node_document(node_doc, base_url=WORKER)
    assert snap is not None
    assert len(snap.gpus) == 1
    assert snap.gpus[0].rebar is None
    assert snap.gpus[0].ggml_device is not None
    assert snap.gpus[0].ggml_device.rebar is None


# -- 2. Pure bandwidth, baseline delta & device telemetry math ---------------


def test_effective_bandwidth_and_baseline_delta_math() -> None:
    # 50 tok/s * 5.6e9 bytes / 320 GB/s = 280 GB/s -> 87.5%
    bw = compute_effective_bandwidth_gbps(50.0, 5_600_000_000)
    assert bw == pytest.approx(280.0, rel=1e-6)
    pct = compute_effective_bandwidth_pct(50.0, 5_600_000_000, 320.0)
    assert pct == pytest.approx(87.5, rel=1e-6)

    # 20 tok/s * 5.6e9 bytes / 320 GB/s = 112 GB/s -> 35.0%
    pct_slow = compute_effective_bandwidth_pct(20.0, 5_600_000_000, 320.0)
    assert pct_slow == pytest.approx(35.0, rel=1e-6)

    # Invalid / non-positive / boolean inputs return None (never fabricated).
    assert compute_effective_bandwidth_gbps(0.0, 5_000_000_000) is None
    assert compute_effective_bandwidth_pct(-5.0, 5_000_000_000, 320.0) is None
    assert compute_effective_bandwidth_pct(50.0, 5_000_000_000, 0.0) is None
    assert compute_effective_bandwidth_pct(True, 5_000_000_000, 320.0) is None  # type: ignore[arg-type]

    assert compute_baseline_delta_pct(25.0, 50.0) == pytest.approx(-50.0, rel=1e-6)
    assert compute_baseline_delta_pct(60.0, 50.0) == pytest.approx(20.0, rel=1e-6)
    assert compute_baseline_delta_pct(25.0, 0.0) is None

    assert known_gpu_spec_bandwidth_gbps("NVIDIA GeForce RTX 5070 Ti") == pytest.approx(896.0)
    assert known_gpu_spec_bandwidth_gbps("AMD Radeon RX 9060 XT") == pytest.approx(320.0)
    assert known_gpu_spec_bandwidth_gbps("AMD Radeon(TM) 780M Graphics") is None
    assert known_gpu_spec_bandwidth_gbps("Unknown Accelerator 9000") is None
    # Laptop / Mobile / GRE variants must NOT map to desktop spec bandwidths.
    assert known_gpu_spec_bandwidth_gbps("NVIDIA GeForce RTX 5090 Laptop GPU") is None
    assert known_gpu_spec_bandwidth_gbps("NVIDIA GeForce RTX 4090 Laptop") is None
    assert known_gpu_spec_bandwidth_gbps("AMD Radeon RX 9070 GRE") is None


def test_device_telemetry_catches_vram_eviction_host_spill_pcie_and_rebar() -> None:
    healthy_gpu = parse_node_gpu_device(_nat08_gpu_json())
    assert healthy_gpu is not None
    ok = evaluate_gpu_device_telemetry([healthy_gpu])
    assert ok.degraded is False
    assert ok.reasons == ()
    assert ok.spec_bandwidth_gbps == pytest.approx(320.0)

    # VRAM budget exceeded (usage > budget on device_local heap -> active eviction).
    evicting = parse_node_gpu_device(_nat08_gpu_json(budget_bytes=12 * GIB, usage_bytes=14 * GIB))
    assert evicting is not None
    evict_res = evaluate_gpu_device_telemetry([evicting])
    assert evict_res.degraded is True
    assert "vram_budget_exceeded" in evict_res.reasons

    # Host-spilled buffers (device_local near/at budget + large host heap usage).
    spilled = parse_node_gpu_device(
        _nat08_gpu_json(budget_bytes=15 * GIB, usage_bytes=int(14.5 * GIB), host_heap_usage_bytes=2 * GIB)
    )
    assert spilled is not None
    spill_res = evaluate_gpu_device_telemetry([spilled])
    assert spill_res.degraded is True
    assert "host_spill_detected" in spill_res.reasons

    # An x16 card in an x4/x8-wired slot (OCuLink / chipset slot) must NOT degrade
    # without an explicit expected_pcie_width baseline; it emits a warning only.
    oculink_x4 = parse_node_gpu_device(_nat08_gpu_json(current_width=4, max_width=16))
    assert oculink_x4 is not None
    oculink_res = evaluate_gpu_device_telemetry([oculink_x4])
    assert oculink_res.degraded is False
    assert "pcie_width_below_max" in oculink_res.warnings
    assert "pcie_width_downgraded" not in oculink_res.reasons

    # With an explicit expected_pcie_width baseline, dropping below it degrades.
    width_drop = parse_node_gpu_device(_nat08_gpu_json(current_width=1, max_width=16))
    assert width_drop is not None
    width_res = evaluate_gpu_device_telemetry(
        [width_drop], baseline=NodeHealthBaseline(expected_pcie_width=4)
    )
    assert width_res.degraded is True
    assert "pcie_width_downgraded" in width_res.reasons

    # Idle PCIe Gen1 (current_gen < max_gen) alone does NOT false-positive without expected_pcie_gen!
    idle_gen1 = parse_node_gpu_device(_nat08_gpu_json(current_gen=1, max_gen=4, current_width=4, max_width=4))
    assert idle_gen1 is not None
    idle_res = evaluate_gpu_device_telemetry([idle_gen1])
    assert idle_res.degraded is False
    assert "pcie_gen_below_max" in idle_res.warnings

    # With explicit expected_pcie_gen=4, Gen3 fallback degrades.
    gen3_fallback = parse_node_gpu_device(_nat08_gpu_json(current_gen=3, max_gen=4))
    assert gen3_fallback is not None
    gen_res = evaluate_gpu_device_telemetry(
        [gen3_fallback], baseline=NodeHealthBaseline(expected_pcie_gen=4)
    )
    assert gen_res.degraded is True
    assert "pcie_gen_downgraded" in gen_res.reasons

    # ReBAR disabled when expected.
    no_rebar = parse_node_gpu_device(_nat08_gpu_json(rebar=False))
    assert no_rebar is not None
    rebar_res = evaluate_gpu_device_telemetry(
        [no_rebar], baseline=NodeHealthBaseline(expect_rebar=True)
    )
    assert rebar_res.degraded is True
    assert "rebar_disabled" in rebar_res.reasons

    # Pre-b12171 missing rebar (None) never falsely triggers rebar_disabled.
    unknown_rebar = parse_node_gpu_device(_nat08_gpu_json(rebar=None))
    assert unknown_rebar is not None
    unknown_rebar_res = evaluate_gpu_device_telemetry(
        [unknown_rebar], baseline=NodeHealthBaseline(expect_rebar=True)
    )
    assert unknown_rebar_res.degraded is False


# -- 3. NodeHealthGate state machine: baseline deltas, context guards, recovery --


def test_health_gate_degrades_on_sustained_effective_bw_or_baseline_drop_and_recovers() -> None:
    policy = NodeHealthGatePolicy(
        min_effective_bw_pct=25.0,
        min_tg_ratio=0.55,
        min_pp_ratio=0.55,
        min_samples=3,
        degraded_cooldown_seconds=30.0,
        recovery_samples=2,
    )
    gate = NodeHealthGate(policy=policy)
    gate.set_baseline(
        WORKER,
        NodeHealthBaseline(
            spec_bandwidth_gbps=320.0,
            active_bytes_per_token=5_600_000_000,
            baseline_tg_tps=50.0,
            baseline_pp_tps=1800.0,
            max_baseline_context_tokens=16384,
        ),
    )

    # Cold load is ignored so warmup never trips the gate.
    v_cold = gate.observe_inference(
        WORKER, now=10.0, decode_tps=8.0, prompt_tps=200.0, output_tokens=64,
        prompt_tokens=512, prompt_uncached_tokens=512, load_state="cold",
    )
    assert v_cold.state == NODE_STATE_UNKNOWN

    # Long-context sample exceeding max_baseline_context_tokens is skipped (context guard).
    v_long = gate.observe_inference(
        WORKER, now=11.0, decode_tps=12.0, prompt_tps=400.0, output_tokens=64,
        prompt_tokens=32768, prompt_uncached_tokens=32768, load_state="warm",
    )
    assert v_long.state == NODE_STATE_UNKNOWN

    # Tiny completion (< min_decode_tokens) does not degrade tg/BW.
    v_tiny = gate.observe_inference(
        WORKER, now=12.0, decode_tps=5.0, output_tokens=4, prompt_tokens=20,
        prompt_uncached_tokens=20, load_state="warm",
    )
    assert v_tiny.state == NODE_STATE_UNKNOWN

    # Sustained degraded throughput (10 tok/s -> 17.5% of 320 GB/s, -80% tg, -75% pp).
    v1 = gate.observe_inference(
        WORKER, now=20.0, decode_tps=10.0, prompt_tps=450.0, output_tokens=64,
        prompt_tokens=512, prompt_uncached_tokens=512, load_state="warm",
    )
    assert v1.state != NODE_STATE_DEGRADED  # streak=1 < min_samples=3
    v2 = gate.observe_inference(
        WORKER, now=21.0, decode_tps=10.0, prompt_tps=450.0, output_tokens=64,
        prompt_tokens=512, prompt_uncached_tokens=512, load_state="warm",
    )
    assert v2.state != NODE_STATE_DEGRADED  # streak=2 < min_samples=3
    v3 = gate.observe_inference(
        WORKER, now=22.0, decode_tps=10.0, prompt_tps=450.0, output_tokens=64,
        prompt_tokens=512, prompt_uncached_tokens=512, load_state="warm",
    )
    assert v3.state == NODE_STATE_DEGRADED
    assert "effective_bandwidth_degraded" in v3.reasons
    assert "tg_baseline_degraded" in v3.reasons
    assert "pp_baseline_degraded" in v3.reasons
    assert v3.effective_bw_pct == pytest.approx(17.5, rel=1e-3)
    assert v3.tg_delta_pct == pytest.approx(-80.0, rel=1e-3)
    assert v3.pp_delta_pct == pytest.approx(-75.0, rel=1e-3)
    assert gate.is_degraded(WORKER, now=25.0) is True

    # After cooldown expires -> probation (allows probe-back).
    v_prob = gate.evaluate(WORKER, now=53.0)
    assert v_prob.state == NODE_STATE_PROBATION
    assert gate.is_degraded(WORKER, now=53.0) is False

    # Two healthy observations recover the node to healthy.
    gate.observe_inference(
        WORKER, now=54.0, decode_tps=48.0, prompt_tps=1750.0, output_tokens=64,
        prompt_tokens=512, prompt_uncached_tokens=512, load_state="warm",
    )
    v_rec = gate.observe_inference(
        WORKER, now=55.0, decode_tps=50.0, prompt_tps=1800.0, output_tokens=64,
        prompt_tokens=512, prompt_uncached_tokens=512, load_state="warm",
    )
    assert v_rec.state == NODE_STATE_HEALTHY
    assert v_rec.reasons == ()


# -- 4. Gateway integration: routes away from degraded nodes -----------------


def _health_doc(
    *,
    models: tuple[str, ...] = ("sonder:latest",),
    gpus: list[dict[str, Any]] | None = None,
    backends: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "status": "ready",
        "api_version": 1,
        "version": "0.2.0",
        "commit": "c",
        "instance_id": "i",
        "synthetic": False,
        "models": [{"id": m, "backend": "llamacpp", "default": i == 0} for i, m in enumerate(models)],
    }
    if gpus is not None:
        doc["gpus"] = gpus
    if backends is not None:
        doc["backends"] = backends
    return doc


def _chat_doc(
    model: str = "sonder:latest",
    *,
    prompt_tps: float = 1800.0,
    decode_tps: float = 80.0,
    prompt_tokens: int = 256,
    completion_tokens: int = 64,
) -> dict[str, Any]:
    return {
        "id": "c",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "timings": {
            "prompt_n": prompt_tokens,
            "prompt_ms": (prompt_tokens / prompt_tps) * 1000.0,
            "prompt_per_second": prompt_tps,
            "predicted_n": completion_tokens,
            "predicted_ms": (completion_tokens / decode_tps) * 1000.0,
            "predicted_per_second": decode_tps,
        },
    }


class _DualHost:
    def __init__(self, *, health: dict[str, dict[str, Any]], chat: dict[str, Any] | None = None) -> None:
        self.health = health
        self.chat = chat or {}
        self.posts: list[str] = []
        self.lock = threading.Lock()

    @staticmethod
    def _base(url: str) -> str:
        return WORKER if url.startswith(WORKER) else PRIMARY

    def get(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        base = self._base(url)
        doc = self.health[base]
        return 200, json.dumps(doc).encode("utf-8")

    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
        base = self._base(url)
        with self.lock:
            self.posts.append(base)
        entry = self.chat.get(base)
        if callable(entry):
            return entry(payload)
        return entry or _chat_doc(payload["model"])


@pytest.fixture
def ca_bundle(tmp_path: Any) -> str:
    path = tmp_path / "private-ca.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n", encoding="utf-8")
    return str(path)


def _gateway_env(ca_bundle: str, **extra: str) -> dict[str, str]:
    env = {
        "SONDER_INFERENCE_BASE_URL": PRIMARY,
        "SONDER_INFERENCE_MODEL": "sonder:latest",
        "SONDER_ALLOW_REMOTE_INFERENCE": "1",
        ENV_PRIVATE_WORKERS: json.dumps([
            {"url": WORKER, "ca_bundle": ca_bundle, "token_env": "SONDER_INFERENCE_NODE1_TOKEN"}
        ]),
        "SONDER_INFERENCE_NODE1_TOKEN": TOKEN,
    }
    env.update(extra)
    return env


def test_gateway_routes_away_from_worker_with_nat08_vram_eviction(ca_bundle: str) -> None:
    hosts = _DualHost(
        health={
            PRIMARY: _health_doc(gpus=[_nat08_gpu_json(name="NVIDIA GeForce RTX 5070 Ti")]),
            WORKER: _health_doc(
                gpus=[_nat08_gpu_json(name="AMD Radeon RX 9060 XT", budget_bytes=12 * GIB, usage_bytes=15 * GIB)]
            ),
        },
    )
    gw = SonderInferenceGateway(
        transport=hosts.post,
        get_transport=hosts.get,
        env=_gateway_env(ca_bundle),
        wall_clock=lambda: datetime(2026, 11, 27, tzinfo=timezone.utc),
    )
    ctx = local_owner_context(correlation_id="fed03-1", source="mcp", timeout_seconds=30.0)
    for prompt in ("a", "b", "c", "d"):
        resp = gw.generate(ModelRequest(prompt=prompt, tier="fast"), ctx)
        assert resp.text == "ok"

    # Every single call must route to PRIMARY; degraded WORKER is skipped before sending work.
    assert hosts.posts == [PRIMARY, PRIMARY, PRIMARY, PRIMARY]
    status = gw.node_health_status()
    assert status[WORKER]["state"] == NODE_STATE_DEGRADED
    assert "vram_budget_exceeded" in status[WORKER]["reasons"]
    assert status[PRIMARY]["state"] == NODE_STATE_HEALTHY


def test_gateway_does_not_degrade_x16_card_in_x4_slot_without_baseline(ca_bundle: str) -> None:
    """An x16 GPU in an x4 OCuLink/chipset slot stays routable with no expected_pcie_width baseline."""
    hosts = _DualHost(
        health={
            PRIMARY: _health_doc(
                gpus=[_nat08_gpu_json(name="NVIDIA GeForce RTX 5070 Ti", current_width=4, max_width=16)]
            ),
            WORKER: _health_doc(gpus=[_nat08_gpu_json(name="AMD Radeon RX 9060 XT")]),
        },
    )
    gw = SonderInferenceGateway(
        transport=hosts.post,
        get_transport=hosts.get,
        env=_gateway_env(ca_bundle),
        wall_clock=lambda: datetime(2026, 11, 27, tzinfo=timezone.utc),
    )
    ctx = local_owner_context(correlation_id="fed03-2a", source="mcp", timeout_seconds=30.0)
    for prompt in ("a", "b"):
        resp = gw.generate(ModelRequest(prompt=prompt, tier="fast"), ctx)
        assert resp.text == "ok"

    assert set(hosts.posts) == {PRIMARY, WORKER}
    status = gw.node_health_status()
    assert status[PRIMARY]["state"] == NODE_STATE_HEALTHY
    assert "pcie_width_below_max" in status[PRIMARY]["warnings"]


def test_gateway_routes_away_from_degraded_primary_with_expected_pcie_width_baseline(ca_bundle: str) -> None:
    baselines = json.dumps({PRIMARY: {"expected_pcie_width": 8}})
    hosts = _DualHost(
        health={
            PRIMARY: _health_doc(
                gpus=[_nat08_gpu_json(name="NVIDIA GeForce RTX 5070 Ti", current_width=1, max_width=16)]
            ),
            WORKER: _health_doc(gpus=[_nat08_gpu_json(name="AMD Radeon RX 9060 XT")]),
        },
    )
    gw = SonderInferenceGateway(
        transport=hosts.post,
        get_transport=hosts.get,
        env=_gateway_env(ca_bundle, **{ENV_NODE_HEALTH_BASELINES: baselines}),
        wall_clock=lambda: datetime(2026, 11, 27, tzinfo=timezone.utc),
    )
    ctx = local_owner_context(correlation_id="fed03-2", source="mcp", timeout_seconds=30.0)
    for prompt in ("a", "b", "c"):
        resp = gw.generate(ModelRequest(prompt=prompt, tier="fast"), ctx)
        assert resp.text == "ok"

    assert hosts.posts == [WORKER, WORKER, WORKER]
    status = gw.node_health_status()
    assert status[PRIMARY]["state"] == NODE_STATE_DEGRADED
    assert "pcie_width_downgraded" in status[PRIMARY]["reasons"]


def test_gateway_routes_away_from_worker_reporting_si_gpu_memory_spill(ca_bundle: str) -> None:
    """Real SI backends[].runtime.gpu_memory (spilled:true / threshold breach / vram_evicted)."""
    spilled_backend = [{
        "name": "llamacpp",
        "available": True,
        "runtime": {
            "gpu_memory": {
                "probe": "win32_pdh",
                "status": "ok",
                "dedicated_bytes": 15 * GIB,
                "shared_bytes": 972 * 1024 * 1024,
                "peak_shared_bytes": 972 * 1024 * 1024,
                "shared_baseline_bytes": 0,
                "spill_threshold_bytes": 256 * 1024 * 1024,
                "spilled": True,
                "samples": 3,
            },
        },
    }]
    clean_backend = [{
        "name": "llamacpp",
        "available": True,
        "runtime": {
            "gpu_memory": {
                "probe": "win32_pdh",
                "status": "ok",
                "dedicated_bytes": 10 * GIB,
                "shared_bytes": 128 * 1024 * 1024,
                "peak_shared_bytes": 128 * 1024 * 1024,
                "shared_baseline_bytes": 0,
                "spill_threshold_bytes": 256 * 1024 * 1024,
                "spilled": False,
                "samples": 3,
            },
        },
    }]
    hosts = _DualHost(
        health={
            PRIMARY: _health_doc(
                gpus=[_nat08_gpu_json(name="NVIDIA GeForce RTX 5070 Ti")],
                backends=clean_backend,
            ),
            WORKER: _health_doc(
                gpus=[_nat08_gpu_json(name="AMD Radeon RX 9060 XT")],
                backends=spilled_backend,
            ),
        },
    )
    gw = SonderInferenceGateway(
        transport=hosts.post,
        get_transport=hosts.get,
        env=_gateway_env(ca_bundle),
        wall_clock=lambda: datetime(2026, 11, 27, tzinfo=timezone.utc),
    )
    ctx = local_owner_context(correlation_id="fed03-spill", source="mcp", timeout_seconds=30.0)
    for prompt in ("a", "b", "c"):
        resp = gw.generate(ModelRequest(prompt=prompt, tier="fast"), ctx)
        assert resp.text == "ok"

    assert hosts.posts == [PRIMARY, PRIMARY, PRIMARY]
    status = gw.node_health_status()
    assert status[WORKER]["state"] == NODE_STATE_DEGRADED
    assert "host_spill_detected" in status[WORKER]["reasons"]
    assert status[PRIMARY]["state"] == NODE_STATE_HEALTHY


def test_gateway_routes_away_on_threshold_breach_and_vram_evicted_and_ignores_unsupported(
    ca_bundle: str,
) -> None:
    # 1. status=='ok' with (shared_bytes - shared_baseline_bytes) > spill_threshold_bytes even if spilled omitted/False
    threshold_breach = [{
        "name": "llamacpp",
        "runtime": {
            "gpu_memory": {
                "probe": "win32_pdh",
                "status": "ok",
                "dedicated_bytes": 15 * GIB,
                "shared_bytes": 512 * 1024 * 1024,
                "shared_baseline_bytes": 128 * 1024 * 1024,
                "spill_threshold_bytes": 256 * 1024 * 1024,
                "spilled": False,
            },
        },
    }]
    unsupported_probe = [{
        "name": "llamacpp",
        "runtime": {
            "gpu_memory": {
                "probe": "none",
                "status": "unsupported",
                "shared_bytes": 999 * 1024 * 1024,
                "shared_baseline_bytes": 0,
                "spill_threshold_bytes": 256 * 1024 * 1024,
                "spilled": False,
            },
        },
    }]
    hosts = _DualHost(
        health={
            PRIMARY: _health_doc(
                gpus=[_nat08_gpu_json(name="NVIDIA GeForce RTX 5070 Ti")],
                backends=unsupported_probe,
            ),
            WORKER: _health_doc(
                gpus=[_nat08_gpu_json(name="AMD Radeon RX 9060 XT")],
                backends=threshold_breach,
            ),
        },
    )
    gw = SonderInferenceGateway(
        transport=hosts.post,
        get_transport=hosts.get,
        env=_gateway_env(ca_bundle),
        wall_clock=lambda: datetime(2026, 11, 27, tzinfo=timezone.utc),
    )
    ctx = local_owner_context(correlation_id="fed03-spill-thresh", source="mcp", timeout_seconds=30.0)
    for prompt in ("a", "b"):
        gw.generate(ModelRequest(prompt=prompt, tier="fast"), ctx)
    assert hosts.posts == [PRIMARY, PRIMARY]
    status = gw.node_health_status()
    assert status[WORKER]["state"] == NODE_STATE_DEGRADED
    assert "host_spill_detected" in status[WORKER]["reasons"]
    assert status[PRIMARY]["state"] == NODE_STATE_HEALTHY

    # 2. residency.vram_evicted == True degrades even without spill.
    evicted_backend = [{
        "name": "llamacpp",
        "runtime": {
            "gpu_memory": {
                "probe": "win32_pdh",
                "status": "ok",
                "spilled": False,
                "residency": {"vram_evicted": True},
            },
        },
    }]
    hosts2 = _DualHost(
        health={
            PRIMARY: _health_doc(gpus=[_nat08_gpu_json(name="NVIDIA GeForce RTX 5070 Ti")]),
            WORKER: _health_doc(
                gpus=[_nat08_gpu_json(name="AMD Radeon RX 9060 XT")],
                backends=evicted_backend,
            ),
        },
    )
    gw2 = SonderInferenceGateway(
        transport=hosts2.post,
        get_transport=hosts2.get,
        env=_gateway_env(ca_bundle),
        wall_clock=lambda: datetime(2026, 11, 27, tzinfo=timezone.utc),
    )
    for prompt in ("a", "b"):
        gw2.generate(ModelRequest(prompt=prompt, tier="fast"), ctx)
    assert hosts2.posts == [PRIMARY, PRIMARY]
    assert "vram_evicted" in gw2.node_health_status()[WORKER]["reasons"]


def test_gateway_routes_away_after_sustained_throughput_drop_via_real_generate_responses(
    ca_bundle: str,
) -> None:
    """Drives _observe_node_health_inference through real generate() calls (no direct gate injection)."""
    baselines = json.dumps({
        PRIMARY: {"baseline_tg_tps": 90.0, "baseline_pp_tps": 2000.0, "spec_bandwidth_gbps": 896.0, "active_bytes_per_token": 5_000_000_000},
        WORKER: {"baseline_tg_tps": 50.0, "baseline_pp_tps": 1800.0, "spec_bandwidth_gbps": 320.0, "active_bytes_per_token": 5_600_000_000},
    })
    assert WORKER in parse_node_health_baselines(baselines)

    hosts = _DualHost(
        health={
            PRIMARY: _health_doc(gpus=[_nat08_gpu_json(name="NVIDIA GeForce RTX 5070 Ti")]),
            WORKER: _health_doc(gpus=[_nat08_gpu_json(name="AMD Radeon RX 9060 XT")]),
        },
        chat={
            PRIMARY: _chat_doc(prompt_tps=2000.0, decode_tps=95.0),
            # Severely degraded worker (8 tok/s -> 14% BW, -84% tg)
            WORKER: _chat_doc(prompt_tps=300.0, decode_tps=8.0),
        },
    )
    gate = NodeHealthGate(
        policy=NodeHealthGatePolicy(min_samples=1, degraded_cooldown_seconds=60.0),
        baselines=parse_node_health_baselines(baselines),
    )
    gw = SonderInferenceGateway(
        transport=hosts.post,
        get_transport=hosts.get,
        env=_gateway_env(ca_bundle, **{ENV_NODE_HEALTH_BASELINES: baselines}),
        wall_clock=lambda: datetime(2026, 11, 27, tzinfo=timezone.utc),
        node_health_gate=gate,
    )
    ctx = local_owner_context(correlation_id="fed03-3", source="mcp", timeout_seconds=30.0)
    # First two calls probe both unmeasured idle endpoints via real generate().
    gw.generate(ModelRequest(prompt="probe-1", tier="fast"), ctx)
    gw.generate(ModelRequest(prompt="probe-2", tier="fast"), ctx)
    assert set(hosts.posts) == {PRIMARY, WORKER}
    assert gw.node_health_status()[WORKER]["state"] == NODE_STATE_DEGRADED
    hosts.posts.clear()

    # Every subsequent call routes away from the degraded WORKER to PRIMARY.
    for prompt in ("a", "b", "c", "d"):
        gw.generate(ModelRequest(prompt=prompt, tier="fast"), ctx)
    assert hosts.posts == [PRIMARY, PRIMARY, PRIMARY, PRIMARY]
    decision = gw._last_routing_decision
    assert isinstance(decision, dict)
    assert WORKER in decision.get("degraded_skipped", ())


def test_gateway_constructor_ignores_malformed_baselines_env_instead_of_crashing(
    ca_bundle: str,
) -> None:
    hosts = _DualHost(
        health={
            PRIMARY: _health_doc(),
            WORKER: _health_doc(),
        },
    )
    gw = SonderInferenceGateway(
        transport=hosts.post,
        get_transport=hosts.get,
        env=_gateway_env(ca_bundle, **{ENV_NODE_HEALTH_BASELINES: "{bad json"}),
        wall_clock=lambda: datetime(2026, 11, 27, tzinfo=timezone.utc),
    )
    ctx = local_owner_context(correlation_id="fed03-bad-env", source="mcp", timeout_seconds=30.0)
    assert gw.generate(ModelRequest(prompt="ok", tier="fast"), ctx).text == "ok"


def test_gateway_falls_back_when_all_nodes_degraded_without_deadlocking(ca_bundle: str) -> None:
    hosts = _DualHost(
        health={
            PRIMARY: _health_doc(
                gpus=[_nat08_gpu_json(name="NVIDIA GeForce RTX 5070 Ti", budget_bytes=10 * GIB, usage_bytes=12 * GIB)]
            ),
            WORKER: _health_doc(
                gpus=[_nat08_gpu_json(name="AMD Radeon RX 9060 XT", budget_bytes=10 * GIB, usage_bytes=12 * GIB)]
            ),
        },
    )
    gw = SonderInferenceGateway(
        transport=hosts.post,
        get_transport=hosts.get,
        env=_gateway_env(ca_bundle),
        wall_clock=lambda: datetime(2026, 11, 27, tzinfo=timezone.utc),
    )
    ctx = local_owner_context(correlation_id="fed03-4", source="mcp", timeout_seconds=30.0)
    resp = gw.generate(ModelRequest(prompt="still serve", tier="fast"), ctx)
    assert resp.text == "ok"
    assert len(hosts.posts) == 1
    assert gw._last_routing_decision["degraded_fallback"] is True


def test_parse_node_health_baselines_fails_closed_on_malformed_input() -> None:
    with pytest.raises(InvalidInput):
        parse_node_health_baselines("{not json")
    with pytest.raises(InvalidInput):
        parse_node_health_baselines(json.dumps([1, 2, 3]))
    with pytest.raises(InvalidInput):
        parse_node_health_baselines(json.dumps({PRIMARY: {"spec_bandwidth_gbps": -10}}))
    with pytest.raises(InvalidInput):
        parse_node_health_baselines(json.dumps({PRIMARY: {"unknown_field": 1}}))
