"""Cross-host RPC split policy enforcement and dedicated-link RTT probe (FED-05).

Builds directly on FED-02's interfaces in
:mod:`sonder_runtime.platform.rpc_sharding_config` (``RPC_LINK_CIDR_ENV``,
``parse_link_network``, ``validate_host_on_link``) and
:mod:`sonder_runtime.adapters.inference.rpc_cluster` (``RpcShardingSettings``,
``RpcPeerEndpoint``), together with the pure domain validators in
:mod:`sonder_runtime.domain.routing.rpc_split_policy`.

FED-02's base allowlist keeps ``SONDER_RPC_LINK_CIDR`` optional (unset = no
link restriction, prefixes 0..32 inside private/link-local ranges). Before a
cross-host split starts, FED-05 adds only the split-specific invariants on top:
1. Confirms ``split_mode == 'layer'``, local output-head placement (device order
   and trailing positive ``tensor_split`` weight), and speculative decoding on.
2. Requires ``SONDER_RPC_LINK_CIDR`` (via ``settings.link_network``, explicit
   ``link_network``, or ``env``) parsed through FED-02's ``parse_link_network``,
   pins prefix length >= /24 via ``validate_dedicated_link_prefix``, and checks
   every peer through FED-02's ``validate_host_on_link``.
3. Measures TCP connect RTT over the dedicated link for every peer (bounded
   samples via injectable connector/clock) and fails closed if any peer is
   unreachable or exceeds the configured RTT budget.
"""
from __future__ import annotations

import ipaddress
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from statistics import median
from typing import Any

from sonder_runtime.adapters.inference.rpc_cluster import (
    DEFAULT_RPC_PORT,
    RpcPeerEndpoint,
    RpcShardingSettings,
    ShardedLlamaServerConfig,
    TcpConnectProbe,
    _default_tcp_connect,
)
from sonder_runtime.domain.routing.rpc_split_policy import (
    MIN_DEDICATED_LINK_PREFIXLEN,
    SplitLoadLogVerdict,
    validate_dedicated_link_prefix,
    validate_split_device_order,
    validate_split_mode,
    validate_split_spec_type,
    validate_split_tensor_weights,
    verify_split_load_log,
)
from sonder_runtime.platform.rpc_sharding_config import (
    RPC_LINK_CIDR_ENV,
    RpcShardingConfigError,
    parse_link_network,
    validate_host_on_link,
)

ENV_RPC_LINK_CIDR = RPC_LINK_CIDR_ENV
ENV_RPC_MAX_RTT_MS = "SONDER_RPC_MAX_RTT_MS"

DEFAULT_MAX_RTT_MS = 2.0
WARN_RTT_MS = 0.5
DEFAULT_RTT_SAMPLES = 5
MAX_RTT_SAMPLES = 20
_MAX_CONFIGURED_RTT_MS = 100.0


def parse_max_rtt_ms(raw: str | None) -> float:
    """Parse ``SONDER_RPC_MAX_RTT_MS``; unset/blank yields ``DEFAULT_MAX_RTT_MS``."""
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_RTT_MS
    cleaned = str(raw).strip()
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise RpcShardingConfigError(
            f"{ENV_RPC_MAX_RTT_MS} must be a positive finite number (ms), got {raw!r}"
        ) from exc
    if not math.isfinite(value) or not (0.0 < value <= _MAX_CONFIGURED_RTT_MS):
        raise RpcShardingConfigError(
            f"{ENV_RPC_MAX_RTT_MS} must be in (0, {_MAX_CONFIGURED_RTT_MS}], got {raw!r}"
        )
    return value


@dataclass(frozen=True)
class CrossHostSplitPolicy:
    """Validated runtime policy thresholds for cross-host RPC splits."""

    max_rtt_ms: float = DEFAULT_MAX_RTT_MS
    warn_rtt_ms: float = WARN_RTT_MS
    rtt_samples: int = DEFAULT_RTT_SAMPLES
    probe_timeout_seconds: float = 2.0
    min_link_prefixlen: int = MIN_DEDICATED_LINK_PREFIXLEN
    require_dedicated_link: bool = True

    def __post_init__(self) -> None:
        for name, lo, hi in (
            ("max_rtt_ms", 0.0, _MAX_CONFIGURED_RTT_MS),
            ("warn_rtt_ms", 0.0, _MAX_CONFIGURED_RTT_MS),
            ("probe_timeout_seconds", 0.01, 30.0),
        ):
            val = getattr(self, name)
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
                raise RpcShardingConfigError(f"{name} must be a finite number")
            if not (lo < float(val) <= hi):
                raise RpcShardingConfigError(f"{name} must be within ({lo}, {hi}]")
        if float(self.warn_rtt_ms) > float(self.max_rtt_ms):
            raise RpcShardingConfigError(
                f"warn_rtt_ms ({self.warn_rtt_ms}) must be <= max_rtt_ms ({self.max_rtt_ms})"
            )
        if type(self.rtt_samples) is not int or not (1 <= self.rtt_samples <= MAX_RTT_SAMPLES):
            raise RpcShardingConfigError(f"rtt_samples must be in 1..{MAX_RTT_SAMPLES}")
        if type(self.min_link_prefixlen) is not int or not (8 <= self.min_link_prefixlen <= 32):
            raise RpcShardingConfigError("min_link_prefixlen must be in 8..32")
        if type(self.require_dedicated_link) is not bool:
            raise RpcShardingConfigError("require_dedicated_link must be a boolean")


@dataclass(frozen=True)
class RpcPeerRttMeasurement:
    """Bounded RTT statistics measured across TCP probes to one RPC worker."""

    endpoint: str
    reachable: bool
    samples_ms: tuple[float, ...] = ()
    min_ms: float | None = None
    median_ms: float | None = None
    p95_ms: float | None = None
    max_ms: float | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "reachable": self.reachable,
            "sample_count": len(self.samples_ms),
            "min_ms": self.min_ms,
            "median_ms": self.median_ms,
            "p95_ms": self.p95_ms,
            "max_ms": self.max_ms,
            "error": self.error,
        }


def _percentile_sorted(values: list[float], pct: float) -> float:
    if len(values) == 1:
        return values[0]
    idx = min(len(values) - 1, max(0, int(math.ceil((pct / 100.0) * len(values))) - 1))
    return values[idx]


def measure_rpc_peer_rtt(
    peer: RpcPeerEndpoint | str,
    *,
    port: int = DEFAULT_RPC_PORT,
    samples: int = DEFAULT_RTT_SAMPLES,
    timeout: float = 2.0,
    connect_fn: TcpConnectProbe | None = None,
    clock: Callable[[], float] | None = None,
) -> RpcPeerRttMeasurement:
    """Measure TCP connect RTT to one allowlisted RPC worker over ``samples`` probes."""
    if type(samples) is not int or not (1 <= samples <= MAX_RTT_SAMPLES):
        raise RpcShardingConfigError(f"samples must be an integer in 1..{MAX_RTT_SAMPLES}")
    resolved = peer if isinstance(peer, RpcPeerEndpoint) else RpcPeerEndpoint.parse(peer, port=port)
    connector = _default_tcp_connect if connect_fn is None else connect_fn
    timer = time.perf_counter if clock is None else clock
    measured: list[float] = []
    for _ in range(samples):
        started = timer()
        try:
            connector((resolved.host, resolved.port), float(timeout))
        except OSError as exc:
            return RpcPeerRttMeasurement(
                endpoint=resolved.address,
                reachable=False,
                samples_ms=tuple(measured),
                error=str(exc) or type(exc).__name__,
            )
        elapsed_ms = max(0.0, (timer() - started) * 1000.0)
        measured.append(round(elapsed_ms, 4))
    ordered = sorted(measured)
    return RpcPeerRttMeasurement(
        endpoint=resolved.address,
        reachable=True,
        samples_ms=tuple(measured),
        min_ms=ordered[0],
        median_ms=round(float(median(ordered)), 4),
        p95_ms=_percentile_sorted(ordered, 95.0),
        max_ms=ordered[-1],
    )


@dataclass(frozen=True)
class CrossHostSplitReport:
    """Structured audit report for FED-05 cross-host split policy compliance."""

    ok: bool
    split_mode: str
    spec_type: str
    link_network: ipaddress.IPv4Network | None
    peer_rtts: tuple[RpcPeerRttMeasurement, ...]
    load_log: SplitLoadLogVerdict | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "split_mode": self.split_mode,
            "spec_type": self.spec_type,
            "link_network": str(self.link_network) if self.link_network is not None else None,
            "peer_rtts": [rtt.to_dict() for rtt in self.peer_rtts],
            "load_log": self.load_log.to_dict() if self.load_log is not None else None,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def _resolve_link_network(
    *,
    link_network: ipaddress.IPv4Network | str | None,
    settings: RpcShardingSettings | None,
    env: Mapping[str, str] | None,
    min_prefixlen: int,
    require_dedicated_link: bool,
) -> tuple[ipaddress.IPv4Network | None, list[str]]:
    errors: list[str] = []
    parsed: ipaddress.IPv4Network | None
    try:
        if isinstance(link_network, ipaddress.IPv4Network):
            parsed = parse_link_network(str(link_network))
        elif isinstance(link_network, str) and link_network.strip():
            parsed = parse_link_network(link_network)
        elif settings is not None and settings.link_network is not None:
            parsed = parse_link_network(str(settings.link_network))
        else:
            source: Mapping[str, str] = os.environ if env is None else env
            parsed = parse_link_network(source.get(RPC_LINK_CIDR_ENV))
    except RpcShardingConfigError as exc:
        errors.append(str(exc))
        return None, errors

    if parsed is None:
        if require_dedicated_link:
            try:
                validate_dedicated_link_prefix(None, min_prefixlen=min_prefixlen)
            except ValueError as exc:
                errors.append(f"{RPC_LINK_CIDR_ENV}: {exc}")
        return None, errors

    try:
        return validate_dedicated_link_prefix(parsed, min_prefixlen=min_prefixlen), errors
    except ValueError as exc:
        errors.append(f"{RPC_LINK_CIDR_ENV}: {exc}")
        return None, errors


def check_cross_host_split_policy(
    config: ShardedLlamaServerConfig,
    *,
    env: Mapping[str, str] | None = None,
    link_network: ipaddress.IPv4Network | str | None = None,
    settings: RpcShardingSettings | None = None,
    policy: CrossHostSplitPolicy | None = None,
    connect_fn: TcpConnectProbe | None = None,
    clock: Callable[[], float] | None = None,
    load_log: str | None = None,
) -> CrossHostSplitReport:
    """Evaluate all FED-05 invariants for ``config`` without raising."""
    source: Mapping[str, str] = os.environ if env is None else env
    errors: list[str] = []
    warnings: list[str] = []
    if policy is None:
        try:
            max_rtt = parse_max_rtt_ms(source.get(ENV_RPC_MAX_RTT_MS))
            active_policy = CrossHostSplitPolicy(
                max_rtt_ms=max_rtt,
                warn_rtt_ms=min(WARN_RTT_MS, max_rtt),
            )
        except RpcShardingConfigError as exc:
            errors.append(str(exc))
            active_policy = CrossHostSplitPolicy()
    else:
        active_policy = policy

    split_mode = config.split_mode
    try:
        split_mode = validate_split_mode(config.split_mode)
    except ValueError as exc:
        errors.append(str(exc))

    spec_type = config.spec_type
    try:
        spec_type = validate_split_spec_type(config.spec_type)
    except ValueError as exc:
        errors.append(str(exc))

    try:
        validate_split_device_order(config.devices)
    except ValueError as exc:
        errors.append(str(exc))

    try:
        validate_split_tensor_weights(config.tensor_split, peer_count=len(config.rpc_peers))
    except ValueError as exc:
        errors.append(str(exc))

    resolved_link, link_errors = _resolve_link_network(
        link_network=link_network,
        settings=settings,
        env=source,
        min_prefixlen=active_policy.min_link_prefixlen,
        require_dedicated_link=active_policy.require_dedicated_link,
    )
    errors.extend(link_errors)

    rtts: list[RpcPeerRttMeasurement] = []
    skip_rtt_probes = resolved_link is None and active_policy.require_dedicated_link
    if not skip_rtt_probes:
        for peer in config.rpc_peers:
            try:
                validate_host_on_link(peer.host, resolved_link, label="RPC peer host")
            except RpcShardingConfigError as exc:
                errors.append(str(exc))
                continue
            rtt = measure_rpc_peer_rtt(
                peer,
                samples=active_policy.rtt_samples,
                timeout=active_policy.probe_timeout_seconds,
                connect_fn=connect_fn,
                clock=clock,
            )
            rtts.append(rtt)
            if not rtt.reachable or rtt.median_ms is None:
                errors.append(
                    f"RPC peer {peer.address} failed RTT probe ({rtt.error or 'unreachable'})"
                )
            elif rtt.median_ms > active_policy.max_rtt_ms:
                errors.append(
                    f"RPC peer {peer.address} measured median RTT {rtt.median_ms:.3f} ms exceeds "
                    f"policy ceiling {active_policy.max_rtt_ms:.3f} ms"
                )
            elif rtt.median_ms > active_policy.warn_rtt_ms:
                warnings.append(
                    f"RPC peer {peer.address} median RTT {rtt.median_ms:.3f} ms exceeds "
                    f"direct-link target {active_policy.warn_rtt_ms:.3f} ms"
                )

    log_verdict: SplitLoadLogVerdict | None = None
    if load_log is not None:
        log_verdict = verify_split_load_log(load_log)
        if not log_verdict.ok:
            errors.extend(log_verdict.errors)

    return CrossHostSplitReport(
        ok=not errors,
        split_mode=split_mode,
        spec_type=spec_type,
        link_network=resolved_link,
        peer_rtts=tuple(rtts),
        load_log=log_verdict,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def require_cross_host_split_policy(
    config: ShardedLlamaServerConfig,
    *,
    env: Mapping[str, str] | None = None,
    link_network: ipaddress.IPv4Network | str | None = None,
    settings: RpcShardingSettings | None = None,
    policy: CrossHostSplitPolicy | None = None,
    connect_fn: TcpConnectProbe | None = None,
    clock: Callable[[], float] | None = None,
    load_log: str | None = None,
) -> CrossHostSplitReport:
    """Run :func:`check_cross_host_split_policy` and raise ``RpcShardingConfigError`` on failure."""
    report = check_cross_host_split_policy(
        config,
        env=env,
        link_network=link_network,
        settings=settings,
        policy=policy,
        connect_fn=connect_fn,
        clock=clock,
        load_log=load_log,
    )
    if not report.ok:
        raise RpcShardingConfigError(
            "cross-host RPC split policy refused:\n"
            + "\n".join(f"- {error}" for error in report.errors)
        )
    return report


__all__ = [
    "CrossHostSplitPolicy",
    "CrossHostSplitReport",
    "DEFAULT_MAX_RTT_MS",
    "DEFAULT_RTT_SAMPLES",
    "ENV_RPC_LINK_CIDR",
    "ENV_RPC_MAX_RTT_MS",
    "MAX_RTT_SAMPLES",
    "RpcPeerRttMeasurement",
    "WARN_RTT_MS",
    "check_cross_host_split_policy",
    "measure_rpc_peer_rtt",
    "parse_max_rtt_ms",
    "require_cross_host_split_policy",
]
