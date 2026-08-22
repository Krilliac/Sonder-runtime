"""Environment-backed native and virtual context sizing policy."""

import os
import re


_QUANTISED_KV = ("q8_0", "q4_0", "q4_1", "q5_0", "q5_1")
DEFAULT_CONTEXT_QUANTISED_KV = 32768
DEFAULT_CONTEXT_FP16_KV = 8192


def _kv_cache_is_quantised() -> bool:
    kind = str(os.environ.get("OLLAMA_KV_CACHE_TYPE", "")).strip().lower()
    return kind in _QUANTISED_KV


def default_context() -> int:
    if _kv_cache_is_quantised():
        return DEFAULT_CONTEXT_QUANTISED_KV
    return DEFAULT_CONTEXT_FP16_KV


DEFAULT_CONTEXT = default_context()
DEFAULT_NATIVE_MAX = 262144
DEFAULT_VIRTUAL_MAX = 1_000_000
MIN_CONTEXT = 512


def parse_strict(value):
    """Parse a size token, returning ``None`` for invalid input."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    text = str(value).strip().lower().replace("_", "").replace(",", "")
    if not text:
        return None
    match = re.match(r"^(\d+(?:\.\d+)?)(k|m)?$", text)
    if not match:
        return None
    number = float(match.group(1))
    if match.group(2) == "k":
        number *= 1000
    elif match.group(2) == "m":
        number *= 1000000
    parsed = int(number)
    return parsed if parsed > 0 else None


def parse_size(value, default=DEFAULT_CONTEXT):
    if value is None:
        return int(default)
    if isinstance(value, (int, float)):
        return max(1, int(value))
    text = str(value).strip().lower().replace("_", "").replace(",", "")
    if not text:
        return int(default)
    match = re.match(r"^(\d+(?:\.\d+)?)(k|m)?$", text)
    if not match:
        return int(default)
    number = float(match.group(1))
    if match.group(2) == "k":
        number *= 1000
    elif match.group(2) == "m":
        number *= 1000000
    return max(1, int(number))


def native_max():
    return parse_size(os.environ.get("SONDER_NATIVE_CONTEXT_MAX"), DEFAULT_NATIVE_MAX)


def virtual_max():
    return parse_size(os.environ.get("SONDER_VIRTUAL_CONTEXT_MAX"), DEFAULT_VIRTUAL_MAX)


def default_requested():
    return parse_size(
        os.environ.get("SONDER_CONTEXT_SIZE")
        or os.environ.get("SONDER_SESSION_NUM_CTX"),
        default_context(),
    )


def requested(value=None):
    raw = default_requested() if value in (None, "") else parse_size(value, default_requested())
    return max(1, min(raw, virtual_max()))


def native(value=None):
    return max(1, min(requested(value), native_max()))


def policy(value=None):
    req = requested(value)
    nat = native(req)
    return {
        "requested": req,
        "native": nat,
        "native_max": native_max(),
        "virtual_max": virtual_max(),
        "virtual": req > nat,
        "mode": "virtual" if req > nat else "native",
    }


def format_policy(value=None):
    values = policy(value)
    lines = [
        "context policy",
        "  requested: %(requested)s tokens" % values,
        "  ollama native num_ctx: %(native)s tokens" % values,
        "  mode: %(mode)s" % values,
        "  native max: %(native_max)s" % values,
        "  virtual max: %(virtual_max)s" % values,
    ]
    if values["virtual"]:
        lines.append(
            "  trick: prompts are kept within native num_ctx while summaries, "
            "retrieval, facts, and recent turns represent the larger virtual budget."
        )
    return "\n".join(lines)
