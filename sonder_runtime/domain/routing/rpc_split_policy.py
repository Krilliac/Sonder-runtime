"""Pure cross-host RPC split policy and load-log verification (FED-05).

Enforces the measured cross-host split invariants verified against
``llama.cpp v1.0.0`` (``src/llama.cpp`` + ``src/llama-model.cpp`` +
``common/speculative.cpp``):

1. **Layer split only** (``--split-mode layer``): ``-sm tensor`` over RPC is
   latency-floored (slower than layer split even at 0.25 ms RTT, #26610) and
   ``-sm row`` was removed from CUDA (#24216).
2. **Logits / output head on the local node**: under ``LLAMA_SPLIT_MODE_LAYER``,
   ``dev_output = get_layer_buft_list(n_layer_all)`` picks the device whose
   cumulative ``tensor_split`` fraction exceeds ``n_layer / (n_layer + 1)``
   via ``std::upper_bound``. Therefore RPC devices must precede local GPUs
   (default llama.cpp order puts RPC devices first; explicit ``--device`` lists
   must keep a local device last), and any explicit ``--tensor-split`` must end
   with a strictly positive weight for the local node (necessary but not
   sufficient: a tiny trailing weight such as ``1,0.001`` can still place
   ``output.weight`` on an RPC device, and one RPC endpoint may expose multiple
   ``RPC<idx>[endpoint]`` devices). :func:`verify_split_load_log` is the
   authoritative check against ``llama-server -lv 4`` load logs.
3. **Speculative decoding on** (default ``ngram-mod``): verifies ``k+1`` tokens
   per cross-host round trip to amortise Ethernet RTT.
4. **Dedicated private link CIDR**: requires a private/link-local IPv4 subnet
   narrower than or equal to ``/24`` (recommended ``/30`` point-to-point) so
   cross-host RPC never traverses a shared LAN/VPN/WAN segment.

Nothing here performs network, disk, or environment I/O.
"""
from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_SPLIT_MODE = "layer"
ALLOWED_SPLIT_MODES: frozenset[str] = frozenset({"layer"})

DEFAULT_SPLIT_SPEC_TYPE = "ngram-mod"
# Verified against llama.cpp v1.0.0 common/speculative.cpp (all non-"none" types).
ALLOWED_SPLIT_SPEC_TYPES: frozenset[str] = frozenset({
    "ngram-mod",
    "ngram-simple",
    "ngram-map-k",
    "ngram-map-k4v",
    "ngram-cache",
    "draft-simple",
    "draft-eagle3",
    "draft-mtp",
    "draft-dflash",
    "draft-dspark",
})

MIN_DEDICATED_LINK_PREFIXLEN = 24
_PRIVATE_LINK_ROOTS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
)

_DEVICE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63}\Z")
_USING_DEVICE_RE = re.compile(
    r"using device\s+([A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63})(?=$|[\s,(])"
)
_LAYER_ASSIGN_RE = re.compile(
    r"load_tensors:\s+layer\s+(\d+)\s+assigned to device\s+([A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63})(?=$|[\s,(])"
)
_MAX_LOG_BYTES = 2 * 1024 * 1024


def is_rpc_device_name(device: str) -> bool:
    """True when a llama.cpp device name identifies a remote ggml-rpc peer."""
    cleaned = str(device or "").strip()
    return cleaned.upper().startswith("RPC")


def validate_split_mode(mode: object) -> str:
    """Require ``split_mode == 'layer'``; refuse tensor/row/none modes."""
    cleaned = str(mode or "").strip().lower() if isinstance(mode, str) else ""
    if cleaned == "tensor":
        raise ValueError(
            "cross-host RPC split_mode='tensor' is refused: tensor parallelism over "
            "Ethernet is latency-floored; use split_mode='layer' only"
        )
    if cleaned == "row":
        raise ValueError(
            "cross-host RPC split_mode='row' is refused: row split was removed from CUDA "
            "(#24216); use split_mode='layer' only"
        )
    if cleaned not in ALLOWED_SPLIT_MODES:
        raise ValueError(
            f"cross-host RPC split_mode must be 'layer' only, got {mode!r}"
        )
    return cleaned


def validate_split_spec_type(spec_type: object) -> str:
    """Require a valid non-'none' llama.cpp v1.0.0 speculative decoding type."""
    cleaned = str(spec_type or "").strip().lower() if isinstance(spec_type, str) else ""
    if not cleaned or cleaned in ("none", "off", "false", "0"):
        raise ValueError(
            "cross-host RPC split requires speculative decoding enabled (e.g. spec_type='ngram-mod') "
            "to amortise network round trips; got %r" % (spec_type,)
        )
    if cleaned not in ALLOWED_SPLIT_SPEC_TYPES:
        raise ValueError(
            f"cross-host RPC spec_type must be one of {sorted(ALLOWED_SPLIT_SPEC_TYPES)}, got {spec_type!r}"
        )
    return cleaned


def validate_split_device_order(devices: Sequence[str]) -> tuple[str, ...]:
    """Validate an explicit ``--device`` order so logits/output head stay local.

    In llama.cpp v1.0.0, ``LLAMA_SPLIT_MODE_LAYER`` assigns the output head
    (``n_layer_all``) to the last device with positive weight. Every ``RPC*``
    device must therefore precede all local GPU devices, and at least one local
    device must appear last.
    """
    if isinstance(devices, (str, bytes)):
        raise ValueError("devices must be a sequence of device names, not a string")
    cleaned: list[str] = []
    for raw in devices:
        if not isinstance(raw, str):
            raise ValueError(f"device name must be a string, got {raw!r}")
        token = raw.strip()
        if not token or not _DEVICE_TOKEN_RE.fullmatch(token):
            raise ValueError(f"invalid device token {raw!r}")
        if token in cleaned:
            raise ValueError(f"duplicate device {token!r} in split device list")
        cleaned.append(token)
    if not cleaned:
        return ()
    if is_rpc_device_name(cleaned[-1]):
        raise ValueError(
            f"cross-host RPC device order {cleaned!r} places remote device {cleaned[-1]!r} last, "
            "which puts the output head / logits over the network; place RPC devices first "
            "and a local GPU last"
        )
    seen_local = False
    for dev in cleaned:
        if is_rpc_device_name(dev):
            if seen_local:
                raise ValueError(
                    f"cross-host RPC device order {cleaned!r} places local device before RPC device "
                    f"{dev!r}; RPC devices must precede local GPUs so the output head stays local"
                )
        else:
            seen_local = True
    if not seen_local:
        raise ValueError("cross-host RPC device list must include at least one local device")
    return tuple(cleaned)


def validate_split_tensor_weights(tensor_split: str, *, peer_count: int) -> str:
    """Validate ``--tensor-split`` weights as a necessary guard for local output head.

    When non-empty, requires at least ``peer_count + 1`` finite non-negative
    weights with a positive sum and a strictly positive final weight (for the
    local GPU). Note that under llama.cpp v1.0.0 ``upper_bound`` cumulative
    splitting, a tiny trailing weight (e.g. ``1,0.001``) can still place the
    output layer on an RPC device and one endpoint can expose multiple
    ``RPC<idx>[endpoint]`` devices; treat this as necessary-not-sufficient and
    use :func:`verify_split_load_log` on ``llama-server -lv 4`` output to
    confirm actual output-head placement.
    """
    if type(peer_count) is not int or peer_count < 1:
        raise ValueError(f"peer_count must be a positive integer, got {peer_count!r}")
    raw = str(tensor_split or "").strip()
    if not raw:
        return ""
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"invalid empty weight in tensor_split={tensor_split!r}")
    weights: list[float] = []
    for part in parts:
        try:
            value = float(part)
        except ValueError as exc:
            raise ValueError(f"invalid weight {part!r} in tensor_split={tensor_split!r}") from exc
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(
                f"tensor_split weights must be finite and non-negative, got {tensor_split!r}"
            )
        weights.append(value)
    if sum(weights) <= 0.0:
        raise ValueError(f"tensor_split must have a positive sum, got {tensor_split!r}")
    if len(weights) < peer_count + 1:
        raise ValueError(
            f"tensor_split={tensor_split!r} has {len(weights)} weight(s), but cross-host split "
            f"with {peer_count} RPC peer(s) requires at least {peer_count + 1} weights (RPC peers + local GPU)"
        )
    if weights[-1] <= 0.0:
        raise ValueError(
            f"tensor_split={tensor_split!r} gives the trailing local device weight {weights[-1]}; "
            "the local node weight must be strictly positive so the output head / logits stay local"
        )
    return ",".join(parts)


def validate_dedicated_link_cidr(
    raw: str | None,
    *,
    min_prefixlen: int = MIN_DEDICATED_LINK_PREFIXLEN,
) -> ipaddress.IPv4Network:
    """Validate a dedicated private/link-local IPv4 CIDR (e.g. ``10.77.0.0/30``)."""
    if type(min_prefixlen) is not int or not (8 <= min_prefixlen <= 32):
        raise ValueError("min_prefixlen must be an integer in 8..32")
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise ValueError("dedicated RPC link CIDR must be configured (e.g. 10.77.0.0/30)")
    try:
        network = ipaddress.ip_network(text, strict=True)
    except ValueError as exc:
        raise ValueError(f"invalid dedicated RPC link CIDR {raw!r}: {exc}") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError(f"dedicated RPC link CIDR must be IPv4, got {raw!r}")
    if not any(network.subnet_of(root) for root in _PRIVATE_LINK_ROOTS):
        raise ValueError(
            f"dedicated RPC link CIDR {network} is not inside a private (RFC 1918) "
            "or link-local (169.254.0.0/16) IPv4 range"
        )
    if network.prefixlen < min_prefixlen:
        raise ValueError(
            f"dedicated RPC link CIDR {network} (/{network.prefixlen}) is broader than "
            f"/{min_prefixlen}; pin a dedicated point-to-point subnet (e.g. /30)"
        )
    return network


@dataclass(frozen=True)
class SplitLoadLogVerdict:
    """Verification result for llama.cpp v1.0.0 cross-host split load logs."""

    ok: bool
    device_order: tuple[str, ...]
    output_device: str | None
    output_is_local: bool | None
    layer_counts_by_device: tuple[tuple[str, int], ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "device_order": list(self.device_order),
            "output_device": self.output_device,
            "output_is_local": self.output_is_local,
            "layer_counts_by_device": dict(self.layer_counts_by_device),
            "errors": list(self.errors),
        }


def verify_split_load_log(log_text: str) -> SplitLoadLogVerdict:
    """Verify from llama.cpp load logs that output head / logits landed on a local device.

    Parses ``using device <DEV>`` lines and ``load_tensors: layer <N> assigned to device <DEV>``
    lines emitted by ``llama.cpp v1.0.0`` (``src/llama.cpp`` and ``src/llama-model.cpp``).
    Fails closed when no device/layer assignment evidence is present.
    """
    text = str(log_text or "")
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_LOG_BYTES:
        text = encoded[:_MAX_LOG_BYTES].decode("utf-8", errors="ignore")

    device_order: list[str] = []
    for match in _USING_DEVICE_RE.finditer(text):
        dev = match.group(1)
        if dev not in device_order:
            device_order.append(dev)

    highest_layer = -1
    output_device: str | None = None
    counts: dict[str, int] = {}
    for match in _LAYER_ASSIGN_RE.finditer(text):
        layer_idx = int(match.group(1))
        dev = match.group(2)
        counts[dev] = counts.get(dev, 0) + 1
        if layer_idx >= highest_layer:
            highest_layer = layer_idx
            output_device = dev

    errors: list[str] = []
    if not device_order and output_device is None:
        errors.append("load log contains no llama.cpp device or layer assignment lines")
    if device_order:
        try:
            validate_split_device_order(device_order)
        except ValueError as exc:
            errors.append(str(exc))

    output_is_local: bool | None = None
    if output_device is None:
        errors.append(
            "load log contains no 'load_tensors: layer N assigned to device' lines to verify "
            "output-head placement (run llama-server with -lv 4)"
        )
    else:
        output_is_local = not is_rpc_device_name(output_device)
        if not output_is_local:
            errors.append(
                f"output layer {highest_layer} (logits/output head) was assigned to remote device "
                f"{output_device!r} instead of the local node"
            )

    return SplitLoadLogVerdict(
        ok=not errors,
        device_order=tuple(device_order),
        output_device=output_device,
        output_is_local=output_is_local,
        layer_counts_by_device=tuple(sorted(counts.items())),
        errors=tuple(errors),
    )


__all__ = [
    "ALLOWED_SPLIT_MODES",
    "ALLOWED_SPLIT_SPEC_TYPES",
    "DEFAULT_SPLIT_MODE",
    "DEFAULT_SPLIT_SPEC_TYPE",
    "MIN_DEDICATED_LINK_PREFIXLEN",
    "SplitLoadLogVerdict",
    "is_rpc_device_name",
    "validate_dedicated_link_cidr",
    "validate_split_device_order",
    "validate_split_mode",
    "validate_split_spec_type",
    "validate_split_tensor_weights",
    "verify_split_load_log",
]
