"""Pure per-node health gate for federated inference routing (FED-03).

Evaluates two complementary, content-free signals per endpoint:

1. **NAT-08 hardware/device telemetry** from ``/v1/sonder/health`` or
   ``/v1/sonder/node`` ``gpus[]``:
   - Vulkan ``VK_EXT_memory_budget`` over-commit (``usage_bytes > budget_bytes``)
     indicating active VRAM eviction on a discrete GPU;
   - host-spilled buffer pressure (discrete VRAM heap saturated alongside large
     host-visible heap usage, or explicit host-spill byte telemetry);
   - PCIe lane-width downgrade (``current_width < max_width`` or configured
     expectation), explicit PCIe generation regression, and unexpected ReBAR loss.
2. **Sustained inference throughput vs hardware/baseline expectations**:
   - effective memory bandwidth percentage:
     ``100 * (decode_tok_per_s * active_bytes_per_token) / (spec_bandwidth_gbps * 1e9)``;
   - prompt-eval (``pp``) and token-generation (``tg``) deltas against configured
     per-node baselines, guarded against context-dependent false positives
     (cold loads, tiny completions/prompts, KV-cache hits, and long-context
     requests beyond the baseline context ceiling are excluded; degradation
     requires ``min_samples`` consecutive low readings).

Nothing here performs network, disk, or environment I/O.
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sonder_runtime.domain.inference_profiles import NodeGpuDevice

NODE_STATE_UNKNOWN = "unknown"
NODE_STATE_HEALTHY = "healthy"
NODE_STATE_DEGRADED = "degraded"
NODE_STATE_PROBATION = "probation"

# Documented decimal memory bandwidth ceilings (GB/s = 10^9 B/s) for discrete GPUs
# used in Sonder deployments. Unknown GPUs and integrated UMA iGPUs return None
# so effective-bandwidth checks never fabricate a denominator.
KNOWN_GPU_SPEC_BANDWIDTH_GBPS: tuple[tuple[str, float], ...] = (
    ("rtx 5090", 1792.0),
    ("rtx 5080", 960.0),
    ("rtx 5070 ti", 896.0),
    ("rtx 5070", 672.0),
    ("rtx 4090", 1008.0),
    ("rtx 4080 super", 736.0),
    ("rtx 4080", 716.8),
    ("rx 9070 xt", 640.0),
    ("rx 9070", 640.0),
    ("rx 9060 xt", 320.0),
)

_MAX_NODES_LIMIT = 256
_MAX_BANDWIDTH_GBPS = 100_000.0
_MAX_ACTIVE_BYTES = 1024 * 1024 * 1024 * 1024  # 1 TiB defensive ceiling
_MAX_TPS = 1_000_000.0


def _positive_finite(value: object, *, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0.0 or numeric > maximum:
        return None
    return numeric


def _positive_int(value: object, *, maximum: int) -> int | None:
    if type(value) is not int or not (1 <= value <= maximum):
        return None
    return value


def known_gpu_spec_bandwidth_gbps(gpu_name: str | None) -> float | None:
    """Return documented discrete GPU memory bandwidth (GB/s), or ``None`` if unknown."""
    if not isinstance(gpu_name, str) or not gpu_name.strip():
        return None
    lowered = " ".join(gpu_name.lower().split())
    for marker, bw_gbps in KNOWN_GPU_SPEC_BANDWIDTH_GBPS:
        if marker in lowered:
            return bw_gbps
    return None


def compute_effective_bandwidth_gbps(
    decode_tps: float,
    active_bytes_per_token: int,
) -> float | None:
    """Return realized decode memory bandwidth in decimal GB/s, or ``None``."""
    tps = _positive_finite(decode_tps, maximum=_MAX_TPS)
    active = _positive_int(active_bytes_per_token, maximum=_MAX_ACTIVE_BYTES)
    if tps is None or active is None:
        return None
    return (tps * float(active)) / 1e9


def compute_effective_bandwidth_pct(
    decode_tps: float,
    active_bytes_per_token: int,
    spec_bandwidth_gbps: float,
) -> float | None:
    """Return ``100 * (tok/s * active_bytes) / (spec_bw_gbps * 1e9)``, or ``None``."""
    bw = compute_effective_bandwidth_gbps(decode_tps, active_bytes_per_token)
    spec = _positive_finite(spec_bandwidth_gbps, maximum=_MAX_BANDWIDTH_GBPS)
    if bw is None or spec is None:
        return None
    return (bw / spec) * 100.0


def compute_baseline_delta_pct(observed: float, baseline: float) -> float | None:
    """Return signed percentage delta ``((observed / baseline) - 1) * 100``, or ``None``."""
    obs = _positive_finite(observed, maximum=_MAX_TPS)
    base = _positive_finite(baseline, maximum=_MAX_TPS)
    if obs is None or base is None:
        return None
    return ((obs / base) - 1.0) * 100.0


@dataclass(frozen=True)
class NodeHealthBaseline:
    """Expected hardware and throughput baseline for one inference endpoint."""

    spec_bandwidth_gbps: float | None = None
    active_bytes_per_token: int | None = None
    baseline_tg_tps: float | None = None
    baseline_pp_tps: float | None = None
    expected_pcie_gen: int | None = None
    expected_pcie_width: int | None = None
    expect_rebar: bool | None = None
    max_baseline_context_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.spec_bandwidth_gbps is not None:
            if _positive_finite(self.spec_bandwidth_gbps, maximum=_MAX_BANDWIDTH_GBPS) is None:
                raise ValueError("spec_bandwidth_gbps must be a positive finite number")
        if self.active_bytes_per_token is not None:
            if _positive_int(self.active_bytes_per_token, maximum=_MAX_ACTIVE_BYTES) is None:
                raise ValueError("active_bytes_per_token must be a positive integer")
        if self.baseline_tg_tps is not None:
            if _positive_finite(self.baseline_tg_tps, maximum=_MAX_TPS) is None:
                raise ValueError("baseline_tg_tps must be a positive finite number")
        if self.baseline_pp_tps is not None:
            if _positive_finite(self.baseline_pp_tps, maximum=_MAX_TPS) is None:
                raise ValueError("baseline_pp_tps must be a positive finite number")
        if self.expected_pcie_gen is not None:
            if _positive_int(self.expected_pcie_gen, maximum=8) is None:
                raise ValueError("expected_pcie_gen must be in 1..8")
        if self.expected_pcie_width is not None:
            if _positive_int(self.expected_pcie_width, maximum=32) is None:
                raise ValueError("expected_pcie_width must be in 1..32")
        if self.expect_rebar is not None and type(self.expect_rebar) is not bool:
            raise ValueError("expect_rebar must be a boolean or None")
        if self.max_baseline_context_tokens is not None:
            if _positive_int(self.max_baseline_context_tokens, maximum=10_000_000) is None:
                raise ValueError("max_baseline_context_tokens must be a positive integer")


@dataclass(frozen=True)
class NodeHealthGatePolicy:
    """Validated thresholds and hysteresis bounds for per-node health gating."""

    min_effective_bw_pct: float = 25.0
    min_tg_ratio: float = 0.55
    min_pp_ratio: float = 0.55
    min_samples: int = 3
    min_decode_tokens: int = 16
    min_uncached_prompt_tokens: int = 64
    vram_budget_overcommit_ratio: float = 1.0
    vram_pressure_ratio: float = 0.95
    host_spill_warn_bytes: int = 512 * 1024 * 1024
    ewma_alpha: float = 0.35
    degraded_cooldown_seconds: float = 60.0
    recovery_samples: int = 2
    max_nodes: int = 64

    def __post_init__(self) -> None:
        for name, lo, hi in (
            ("min_effective_bw_pct", 1.0, 100.0),
            ("min_tg_ratio", 0.05, 1.0),
            ("min_pp_ratio", 0.05, 1.0),
            ("vram_budget_overcommit_ratio", 0.5, 2.0),
            ("vram_pressure_ratio", 0.5, 1.0),
            ("ewma_alpha", 0.05, 1.0),
        ):
            val = getattr(self, name)
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ValueError(f"{name} must be a finite number")
            if not (lo <= float(val) <= hi):
                raise ValueError(f"{name} must be within {lo}..{hi}")
        if (
            isinstance(self.degraded_cooldown_seconds, bool)
            or not isinstance(self.degraded_cooldown_seconds, (int, float))
            or not math.isfinite(self.degraded_cooldown_seconds)
            or not (0.0 <= float(self.degraded_cooldown_seconds) <= 86_400.0)
        ):
            raise ValueError("degraded_cooldown_seconds must be within 0..86400")
        for name, lo, hi in (
            ("min_samples", 1, 100),
            ("min_decode_tokens", 1, 100_000),
            ("min_uncached_prompt_tokens", 1, 1_000_000),
            ("host_spill_warn_bytes", 1, _MAX_ACTIVE_BYTES),
            ("recovery_samples", 1, 100),
            ("max_nodes", 1, _MAX_NODES_LIMIT),
        ):
            val = getattr(self, name)
            if type(val) is not int or not (lo <= val <= hi):
                raise ValueError(f"{name} must be an integer within {lo}..{hi}")


@dataclass(frozen=True)
class DeviceTelemetryHealth:
    """Pure evaluation of NAT-08 GPU device telemetry for one node."""

    degraded: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    spec_bandwidth_gbps: float | None = None
    max_vram_usage_ratio: float | None = None
    driver_build: str | None = None
    rebar: bool | None = None


def evaluate_gpu_device_telemetry(
    gpus: Sequence[NodeGpuDevice],
    *,
    baseline: NodeHealthBaseline | None = None,
    policy: NodeHealthGatePolicy | None = None,
    host_spill_bytes: int | None = None,
) -> DeviceTelemetryHealth:
    """Evaluate NAT-08 discrete GPU telemetry for eviction, spill, PCIe, and ReBAR faults."""
    pol = policy or NodeHealthGatePolicy()
    reasons: list[str] = []
    warnings: list[str] = []
    spec_bw: float | None = baseline.spec_bandwidth_gbps if baseline is not None else None
    max_usage_ratio: float | None = None
    driver_build: str | None = None
    observed_rebar: bool | None = None

    if (
        host_spill_bytes is not None
        and type(host_spill_bytes) is int
        and host_spill_bytes >= pol.host_spill_warn_bytes
    ):
        reasons.append("host_spill_detected")

    discrete_gpus = [gpu for gpu in gpus if not gpu.is_integrated_uma]
    for gpu in discrete_gpus:
        if spec_bw is None:
            spec_bw = known_gpu_spec_bandwidth_gbps(gpu.name)
        if driver_build is None and gpu.driver_build:
            driver_build = gpu.driver_build
        eff_rebar = gpu.rebar
        if eff_rebar is None and gpu.ggml_device is not None:
            eff_rebar = gpu.ggml_device.rebar
        if observed_rebar is None and eff_rebar is not None:
            observed_rebar = eff_rebar
        if baseline is not None and baseline.expect_rebar is True and eff_rebar is False:
            if "rebar_disabled" not in reasons:
                reasons.append("rebar_disabled")

        device_local_pressure = False
        host_heap_usage = 0
        for heap in gpu.vulkan_heaps:
            if heap.device_local:
                if (
                    heap.budget_bytes is not None
                    and heap.budget_bytes > 0
                    and heap.usage_bytes is not None
                ):
                    ratio = float(heap.usage_bytes) / float(heap.budget_bytes)
                    if max_usage_ratio is None or ratio > max_usage_ratio:
                        max_usage_ratio = ratio
                    if ratio > pol.vram_budget_overcommit_ratio:
                        if "vram_budget_exceeded" not in reasons:
                            reasons.append("vram_budget_exceeded")
                    if ratio >= pol.vram_pressure_ratio:
                        device_local_pressure = True
            elif heap.host_visible and heap.usage_bytes is not None:
                host_heap_usage = max(host_heap_usage, heap.usage_bytes)

        if (
            gpu.dedicated_total_mib is not None
            and gpu.dedicated_total_mib > 0
            and gpu.effective_free_vram_mib == 0
        ):
            device_local_pressure = True

        if device_local_pressure and host_heap_usage >= pol.host_spill_warn_bytes:
            if "host_spill_detected" not in reasons:
                reasons.append("host_spill_detected")

        pcie = gpu.pcie
        if pcie is not None:
            if pcie.current_width is not None:
                if (
                    baseline is not None
                    and baseline.expected_pcie_width is not None
                    and pcie.current_width < baseline.expected_pcie_width
                ) or (
                    pcie.max_width is not None and pcie.current_width < pcie.max_width
                ):
                    if "pcie_width_downgraded" not in reasons:
                        reasons.append("pcie_width_downgraded")
            if pcie.current_gen is not None:
                if (
                    baseline is not None
                    and baseline.expected_pcie_gen is not None
                    and pcie.current_gen < baseline.expected_pcie_gen
                ):
                    if "pcie_gen_downgraded" not in reasons:
                        reasons.append("pcie_gen_downgraded")
                elif pcie.max_gen is not None and pcie.current_gen < pcie.max_gen:
                    if "pcie_gen_below_max" not in warnings:
                        warnings.append("pcie_gen_below_max")

    return DeviceTelemetryHealth(
        degraded=bool(reasons),
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        spec_bandwidth_gbps=spec_bw,
        max_vram_usage_ratio=round(max_usage_ratio, 4) if max_usage_ratio is not None else None,
        driver_build=driver_build,
        rebar=observed_rebar,
    )


@dataclass(frozen=True)
class NodeHealthVerdict:
    """Content-free health verdict for one inference endpoint."""

    endpoint: str
    state: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    effective_bw_gbps: float | None = None
    effective_bw_pct: float | None = None
    spec_bandwidth_gbps: float | None = None
    tg_tps: float | None = None
    pp_tps: float | None = None
    tg_delta_pct: float | None = None
    pp_delta_pct: float | None = None
    max_vram_usage_ratio: float | None = None
    driver_build: str | None = None
    rebar: bool | None = None
    sample_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "state": self.state,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "effective_bw_gbps": (
                round(self.effective_bw_gbps, 2) if self.effective_bw_gbps is not None else None
            ),
            "effective_bw_pct": (
                round(self.effective_bw_pct, 2) if self.effective_bw_pct is not None else None
            ),
            "spec_bandwidth_gbps": (
                round(self.spec_bandwidth_gbps, 2) if self.spec_bandwidth_gbps is not None else None
            ),
            "tg_tps": round(self.tg_tps, 2) if self.tg_tps is not None else None,
            "pp_tps": round(self.pp_tps, 2) if self.pp_tps is not None else None,
            "tg_delta_pct": round(self.tg_delta_pct, 2) if self.tg_delta_pct is not None else None,
            "pp_delta_pct": round(self.pp_delta_pct, 2) if self.pp_delta_pct is not None else None,
            "max_vram_usage_ratio": self.max_vram_usage_ratio,
            "driver_build": self.driver_build,
            "rebar": self.rebar,
            "sample_count": self.sample_count,
        }


@dataclass
class _NodeRecord:
    device_health: DeviceTelemetryHealth | None = None
    device_observed_at: float = 0.0
    tg_ewma: float | None = None
    pp_ewma: float | None = None
    last_active_bytes: int | None = None
    sample_count: int = 0
    degraded_streak: int = 0
    healthy_streak: int = 0
    throughput_degraded: bool = False
    throughput_reasons: tuple[str, ...] = ()
    degraded_until: float = 0.0


class NodeHealthGate:
    """Thread-safe, bounded per-endpoint health gate combining NAT-08 and pp/tg signals."""

    def __init__(
        self,
        *,
        policy: NodeHealthGatePolicy | None = None,
        baselines: Mapping[str, NodeHealthBaseline] | None = None,
    ) -> None:
        self._policy = policy or NodeHealthGatePolicy()
        self._lock = threading.RLock()
        self._baselines: dict[str, NodeHealthBaseline] = {}
        if baselines:
            for endpoint, baseline in baselines.items():
                self.set_baseline(endpoint, baseline)
        self._records: OrderedDict[str, _NodeRecord] = OrderedDict()

    @property
    def policy(self) -> NodeHealthGatePolicy:
        return self._policy

    def set_baseline(self, endpoint: str, baseline: NodeHealthBaseline) -> None:
        clean = str(endpoint or "").strip().rstrip("/")
        if not clean:
            raise ValueError("endpoint must not be empty")
        if not isinstance(baseline, NodeHealthBaseline):
            raise TypeError("baseline must be a NodeHealthBaseline")
        with self._lock:
            self._baselines[clean] = baseline

    def _baseline_for(self, endpoint: str) -> NodeHealthBaseline | None:
        clean = str(endpoint or "").strip().rstrip("/")
        return self._baselines.get(clean)

    def _record_for(self, endpoint: str) -> _NodeRecord:
        clean = str(endpoint or "").strip().rstrip("/")
        rec = self._records.get(clean)
        if rec is None:
            rec = _NodeRecord()
            self._records[clean] = rec
            while len(self._records) > self._policy.max_nodes:
                self._records.popitem(last=False)
        else:
            self._records.move_to_end(clean)
        return rec

    def observe_device_telemetry(
        self,
        endpoint: str,
        gpus: Sequence[NodeGpuDevice],
        *,
        now: float,
        host_spill_bytes: int | None = None,
    ) -> NodeHealthVerdict:
        """Fold one NAT-08 GPU inventory snapshot into the endpoint's health verdict."""
        clean = str(endpoint or "").strip().rstrip("/")
        if not clean or not math.isfinite(now):
            return NodeHealthVerdict(endpoint=clean, state=NODE_STATE_UNKNOWN)
        with self._lock:
            baseline = self._baseline_for(clean)
            rec = self._record_for(clean)
            rec.device_health = evaluate_gpu_device_telemetry(
                gpus,
                baseline=baseline,
                policy=self._policy,
                host_spill_bytes=host_spill_bytes,
            )
            rec.device_observed_at = now
            return self._verdict_locked(clean, rec, baseline, now=now)

    def observe_inference(
        self,
        endpoint: str,
        *,
        now: float,
        decode_tps: float | None = None,
        prompt_tps: float | None = None,
        output_tokens: int | None = None,
        prompt_tokens: int | None = None,
        prompt_uncached_tokens: int | None = None,
        load_state: str | None = None,
        active_bytes_per_token: int | None = None,
    ) -> NodeHealthVerdict:
        """Fold one completed inference call's pp/tg measurements into the gate."""
        clean = str(endpoint or "").strip().rstrip("/")
        if not clean or not math.isfinite(now):
            return NodeHealthVerdict(endpoint=clean, state=NODE_STATE_UNKNOWN)
        with self._lock:
            baseline = self._baseline_for(clean)
            rec = self._record_for(clean)
            if isinstance(load_state, str) and load_state.strip().casefold() == "cold":
                return self._verdict_locked(clean, rec, baseline, now=now)
            if (
                baseline is not None
                and baseline.max_baseline_context_tokens is not None
                and type(prompt_tokens) is int
                and prompt_tokens > baseline.max_baseline_context_tokens
            ):
                return self._verdict_locked(clean, rec, baseline, now=now)

            pol = self._policy
            alpha = pol.ewma_alpha
            valid_tg = (
                _positive_finite(decode_tps, maximum=_MAX_TPS)
                if (output_tokens is None or (type(output_tokens) is int and output_tokens >= pol.min_decode_tokens))
                else None
            )
            uncached = prompt_uncached_tokens if type(prompt_uncached_tokens) is int else prompt_tokens
            valid_pp = (
                _positive_finite(prompt_tps, maximum=_MAX_TPS)
                if (uncached is None or (type(uncached) is int and uncached >= pol.min_uncached_prompt_tokens))
                else None
            )
            if valid_tg is None and valid_pp is None:
                return self._verdict_locked(clean, rec, baseline, now=now)

            if valid_tg is not None:
                rec.tg_ewma = valid_tg if rec.tg_ewma is None else (alpha * valid_tg + (1.0 - alpha) * rec.tg_ewma)
            if valid_pp is not None:
                rec.pp_ewma = valid_pp if rec.pp_ewma is None else (alpha * valid_pp + (1.0 - alpha) * rec.pp_ewma)
            if active_bytes_per_token is not None:
                parsed_bytes = _positive_int(active_bytes_per_token, maximum=_MAX_ACTIVE_BYTES)
                if parsed_bytes is not None:
                    rec.last_active_bytes = parsed_bytes
            rec.sample_count += 1

            sample_reasons = self._evaluate_throughput_sample(
                rec, baseline, sample_tg=valid_tg, sample_pp=valid_pp,
            )
            if sample_reasons:
                rec.degraded_streak += 1
                rec.healthy_streak = 0
                if rec.throughput_degraded or rec.degraded_streak >= pol.min_samples:
                    rec.throughput_degraded = True
                    rec.throughput_reasons = sample_reasons
                    rec.degraded_until = now + pol.degraded_cooldown_seconds
            else:
                rec.degraded_streak = 0
                rec.healthy_streak += 1
                if rec.throughput_degraded and rec.healthy_streak >= pol.recovery_samples:
                    rec.throughput_degraded = False
                    rec.throughput_reasons = ()
                    rec.degraded_until = 0.0

            return self._verdict_locked(clean, rec, baseline, now=now)

    def _spec_bandwidth_for(
        self,
        rec: _NodeRecord,
        baseline: NodeHealthBaseline | None,
    ) -> float | None:
        if baseline is not None and baseline.spec_bandwidth_gbps is not None:
            return baseline.spec_bandwidth_gbps
        if rec.device_health is not None and rec.device_health.spec_bandwidth_gbps is not None:
            return rec.device_health.spec_bandwidth_gbps
        return None

    def _active_bytes_for(
        self,
        rec: _NodeRecord,
        baseline: NodeHealthBaseline | None,
    ) -> int | None:
        if baseline is not None and baseline.active_bytes_per_token is not None:
            return baseline.active_bytes_per_token
        return rec.last_active_bytes

    def _evaluate_throughput_sample(
        self,
        rec: _NodeRecord,
        baseline: NodeHealthBaseline | None,
        *,
        sample_tg: float | None,
        sample_pp: float | None,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        pol = self._policy
        spec_bw = self._spec_bandwidth_for(rec, baseline)
        active_bytes = self._active_bytes_for(rec, baseline)
        if sample_tg is not None and spec_bw is not None and active_bytes is not None:
            bw_pct = compute_effective_bandwidth_pct(sample_tg, active_bytes, spec_bw)
            if bw_pct is not None and bw_pct < pol.min_effective_bw_pct:
                reasons.append("effective_bandwidth_degraded")
        if baseline is not None:
            if sample_tg is not None and baseline.baseline_tg_tps is not None:
                if (sample_tg / baseline.baseline_tg_tps) < pol.min_tg_ratio:
                    reasons.append("tg_baseline_degraded")
            if sample_pp is not None and baseline.baseline_pp_tps is not None:
                if (sample_pp / baseline.baseline_pp_tps) < pol.min_pp_ratio:
                    reasons.append("pp_baseline_degraded")
        return tuple(reasons)

    def _verdict_locked(
        self,
        endpoint: str,
        rec: _NodeRecord | None,
        baseline: NodeHealthBaseline | None,
        *,
        now: float,
    ) -> NodeHealthVerdict:
        if rec is None:
            return NodeHealthVerdict(
                endpoint=endpoint,
                state=NODE_STATE_UNKNOWN,
                spec_bandwidth_gbps=baseline.spec_bandwidth_gbps if baseline is not None else None,
            )

        dev = rec.device_health
        spec_bw = self._spec_bandwidth_for(rec, baseline)
        active_bytes = self._active_bytes_for(rec, baseline)
        eff_bw_gbps = (
            compute_effective_bandwidth_gbps(rec.tg_ewma, active_bytes)
            if rec.tg_ewma is not None and active_bytes is not None
            else None
        )
        eff_bw_pct = (
            compute_effective_bandwidth_pct(rec.tg_ewma, active_bytes, spec_bw)
            if rec.tg_ewma is not None and active_bytes is not None and spec_bw is not None
            else None
        )
        tg_delta = (
            compute_baseline_delta_pct(rec.tg_ewma, baseline.baseline_tg_tps)
            if rec.tg_ewma is not None and baseline is not None and baseline.baseline_tg_tps is not None
            else None
        )
        pp_delta = (
            compute_baseline_delta_pct(rec.pp_ewma, baseline.baseline_pp_tps)
            if rec.pp_ewma is not None and baseline is not None and baseline.baseline_pp_tps is not None
            else None
        )

        reasons: list[str] = []
        warnings: list[str] = []
        if dev is not None:
            reasons.extend(dev.reasons)
            warnings.extend(dev.warnings)

        device_degraded = dev is not None and dev.degraded
        throughput_active = rec.throughput_degraded and now < rec.degraded_until
        throughput_probation = rec.throughput_degraded and now >= rec.degraded_until

        if rec.throughput_degraded:
            for reason in rec.throughput_reasons:
                if reason not in reasons:
                    reasons.append(reason)

        if device_degraded or throughput_active:
            state = NODE_STATE_DEGRADED
        elif throughput_probation:
            state = NODE_STATE_PROBATION
        elif dev is not None or rec.sample_count > 0:
            state = NODE_STATE_HEALTHY
        else:
            state = NODE_STATE_UNKNOWN

        return NodeHealthVerdict(
            endpoint=endpoint,
            state=state,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            effective_bw_gbps=eff_bw_gbps,
            effective_bw_pct=eff_bw_pct,
            spec_bandwidth_gbps=spec_bw,
            tg_tps=rec.tg_ewma,
            pp_tps=rec.pp_ewma,
            tg_delta_pct=tg_delta,
            pp_delta_pct=pp_delta,
            max_vram_usage_ratio=dev.max_vram_usage_ratio if dev is not None else None,
            driver_build=dev.driver_build if dev is not None else None,
            rebar=dev.rebar if dev is not None else None,
            sample_count=rec.sample_count,
        )

    def evaluate(self, endpoint: str, *, now: float) -> NodeHealthVerdict:
        clean = str(endpoint or "").strip().rstrip("/")
        with self._lock:
            return self._verdict_locked(
                clean,
                self._records.get(clean),
                self._baseline_for(clean),
                now=now,
            )

    def is_degraded(self, endpoint: str, *, now: float) -> bool:
        return self.evaluate(endpoint, now=now).state == NODE_STATE_DEGRADED

    def filter_routable_endpoints(
        self,
        endpoints: Sequence[str],
        *,
        now: float,
    ) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
        """Partition endpoints into ``(routable, degraded_skipped, all_degraded_fallback)``.

        Whenever at least one candidate is not degraded (healthy, unknown, or in
        cooldown-expired probation), degraded candidates are excluded so traffic
        routes away from degraded nodes. When every candidate is degraded, all
        candidates are returned with ``all_degraded_fallback=True`` so callers do
        not deadlock.
        """
        preferred: list[str] = []
        degraded: list[str] = []
        with self._lock:
            for endpoint in endpoints:
                verdict = self._verdict_locked(
                    str(endpoint or "").strip().rstrip("/"),
                    self._records.get(str(endpoint or "").strip().rstrip("/")),
                    self._baseline_for(endpoint),
                    now=now,
                )
                if verdict.state == NODE_STATE_DEGRADED:
                    degraded.append(endpoint)
                else:
                    preferred.append(endpoint)
        if preferred:
            return tuple(preferred), tuple(degraded), False
        return tuple(endpoints), (), bool(degraded)

    def status(self, *, now: float) -> dict[str, dict[str, object]]:
        """Content-free per-endpoint health verdicts for diagnostics."""
        with self._lock:
            keys = sorted(set(self._records) | set(self._baselines))
            return {
                key: self._verdict_locked(
                    key,
                    self._records.get(key),
                    self._baseline_for(key),
                    now=now,
                ).to_dict()
                for key in keys
            }


__all__ = [
    "DeviceTelemetryHealth",
    "KNOWN_GPU_SPEC_BANDWIDTH_GBPS",
    "NODE_STATE_DEGRADED",
    "NODE_STATE_HEALTHY",
    "NODE_STATE_PROBATION",
    "NODE_STATE_UNKNOWN",
    "NodeHealthBaseline",
    "NodeHealthGate",
    "NodeHealthGatePolicy",
    "NodeHealthVerdict",
    "compute_baseline_delta_pct",
    "compute_effective_bandwidth_gbps",
    "compute_effective_bandwidth_pct",
    "evaluate_gpu_device_telemetry",
    "known_gpu_spec_bandwidth_gbps",
]
