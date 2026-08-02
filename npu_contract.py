"""Provider-neutral accelerator wire contract, limits, and validators.

This module is shared by the stdlib-only host (broker/service) and the
restartable child worker. It must never import vendor runtimes; it defines the
bounded JSONL protocol, the allowlists the host validates against, and the
strict limits every accelerator request and response must obey.

The NPU path is a utility accelerator below the fast/code/general local model
tiers. It is never a generative tier and its failures must fall back to the
existing local behavior, never to cloud.
"""
from __future__ import annotations

import hashlib
import json
import math


PROTOCOL_VERSION = 1

# Bounded stdio framing: one JSON object per line, both directions.
MAX_LINE_BYTES = 1_048_576

# Input/output limits enforced by host and worker independently.
MAX_TEXT_ITEMS = 16
MAX_TEXT_CHARS = 8_000
MAX_FEATURES = 64
MAX_DIMENSION = 4_096

# Deadline clamps per operation (milliseconds).
MIN_DEADLINE_MS = 50
MAX_ROUTE_DEADLINE_MS = 2_000
MAX_EMBED_DEADLINE_MS = 10_000
DEFAULT_ROUTE_DEADLINE_MS = 400
DEFAULT_EMBED_DEADLINE_MS = 2_000

# Worker lifecycle limits (milliseconds unless suffixed otherwise).
READY_TIMEOUT_MS = 20_000
LOAD_TIMEOUT_MS = 60_000
SHUTDOWN_TIMEOUT_MS = 2_000

OPERATIONS = ("routing", "embedding")

# The ambiguous-execution router may only ever score these host-owned modes.
ROUTE_MODES = ("workbench", "autopilot")

# Accelerator responses may carry only these reason codes; anything else is
# rejected by the host so model output can never inject free text.
ROUTE_REASON_CODES = ("score_margin", "low_confidence", "degenerate")

# Provider identifiers the broker/worker understand. "winml" is descriptor-only
# until a supported Python runtime path exists; DirectML is GPU-class and is
# intentionally not claimed as an NPU provider. "cpu" is the onnxruntime CPU
# reference provider (same weights, same identity). "cpu-sim" is the stdlib
# deterministic simulator used for portable CI and explicit opt-in testing.
PROVIDER_IDS = ("vitisai", "openvino", "qnn", "winml", "cpu", "cpu-sim")

# Providers that actually claim NPU silicon when assigned by the runtime.
NPU_CLASS_PROVIDERS = frozenset({"vitisai", "openvino", "qnn"})


def encode_line(payload) -> bytes:
    """Encode one protocol message as bounded, strict, compact JSONL."""
    if not isinstance(payload, dict):
        raise ValueError("protocol message must be a JSON object")
    try:
        text = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("protocol message is not strict JSON: %s" % exc)
    raw = text.encode("utf-8") + b"\n"
    if len(raw) > MAX_LINE_BYTES:
        raise ValueError(
            "protocol message exceeds %d byte line limit" % MAX_LINE_BYTES
        )
    return raw


def _reject_constant(value):
    raise ValueError("non-finite JSON constant is not allowed: %s" % value)


def decode_line(raw) -> dict:
    """Decode one bounded protocol line; reject anything non-strict."""
    if raw is None:
        raise ValueError("empty protocol line")
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "strict")
    line = raw.rstrip(b"\r\n")
    if not line:
        raise ValueError("empty protocol line")
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("protocol line exceeds %d bytes" % MAX_LINE_BYTES)
    try:
        text = line.decode("utf-8", "strict")
        payload = json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("malformed protocol line: %s" % exc)
    if not isinstance(payload, dict):
        raise ValueError("protocol line must decode to a JSON object")
    return payload


def _finite_unit(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % label)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("%s must be finite" % label)
    if not 0.0 <= number <= 1.0:
        raise ValueError("%s must be between 0 and 1" % label)
    return number


def validate_route_scores(payload) -> dict:
    """Validate an accelerator routing response against the host allowlists."""
    if not isinstance(payload, dict):
        raise ValueError("route response must be a JSON object")
    scores = payload.get("scores")
    if not isinstance(scores, dict):
        raise ValueError("route response must carry a scores object")
    if set(scores) != set(ROUTE_MODES):
        raise ValueError(
            "route scores must cover exactly: %s" % ", ".join(ROUTE_MODES)
        )
    validated = {
        mode: _finite_unit(scores[mode], "route score %s" % mode)
        for mode in ROUTE_MODES
    }
    reason_code = str(payload.get("reason_code") or "")
    if reason_code not in ROUTE_REASON_CODES:
        raise ValueError(
            "route reason_code must be one of: %s" % ", ".join(ROUTE_REASON_CODES)
        )
    return {"scores": validated, "reason_code": reason_code}


def validate_vector(values, dimension) -> list:
    """Validate one accelerator output vector: exact size, finite, non-zero."""
    if isinstance(dimension, bool) or not isinstance(dimension, int):
        raise ValueError("vector dimension must be an integer")
    if not 1 <= dimension <= MAX_DIMENSION:
        raise ValueError(
            "vector dimension must be between 1 and %d" % MAX_DIMENSION
        )
    if not isinstance(values, (list, tuple)):
        raise ValueError("vector must be a list of numbers")
    if len(values) != dimension:
        raise ValueError(
            "vector has %d values but the manifest declares %d"
            % (len(values), dimension)
        )
    vector = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("vector values must be numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("vector values must be finite")
        vector.append(number)
    if all(value == 0.0 for value in vector):
        raise ValueError("vector must not be all zeros")
    return vector


def clamp_deadline_ms(value, operation) -> int:
    """Clamp a requested deadline into the per-operation contract bounds."""
    ceiling = (
        MAX_EMBED_DEADLINE_MS if operation == "embedding" else MAX_ROUTE_DEADLINE_MS
    )
    default = (
        DEFAULT_EMBED_DEADLINE_MS
        if operation == "embedding"
        else DEFAULT_ROUTE_DEADLINE_MS
    )
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_DEADLINE_MS, min(ceiling, number))


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
