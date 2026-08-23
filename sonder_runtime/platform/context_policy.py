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


def _parameter_billions(value):
    """Parse Ollama's human-readable parameter size into billions."""
    if value is None:
        return None
    text = str(value).strip().upper().replace(",", "")
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([BM]?)$", text)
    if not match:
        return None
    amount = float(match.group(1))
    if match.group(2) == "M":
        amount /= 1000.0
    return amount if amount > 0 else None


# KV-cache reservation bands by total parameter count (billions).  Larger
# models spend more bytes of KV per token (more layers and wider heads), so
# the auto-selected window shrinks as the weights grow.  The ladder is a
# planning envelope, not a measured residency report.
_PARAMETER_CONTEXT_BANDS = (
    (48.0, 8_192, "parameters>=48B"),
    (24.0, 16_384, "parameters>=24B"),
    (12.0, 24_576, "parameters>=12B"),
)


def auto_context_plan(model_context=None, parameter_size=None) -> dict:
    """Explain a model-aware native context choice with clamp provenance.

    Returns a dict with the selected ``context`` plus the intermediate facts
    that produced it: the starting ``base`` and its ``source``, the parsed
    ``parameter_billions`` and ``advertised`` maximum, and the ordered list of
    ``clamps`` that actually reduced (or raised) the value.  The physical
    limits — the model's advertised maximum, the native ceiling, and the
    minimum window — always apply.  The parameter-band ladder applies only
    when the operator has *not* pinned a size: an explicit
    ``SONDER_CONTEXT_SIZE``/``SONDER_SESSION_NUM_CTX`` is an informed override
    of the conservative KV budget, matching this module's documented contract.
    """
    has_explicit_environment = bool(
        str(os.environ.get("SONDER_CONTEXT_SIZE") or "").strip()
        or str(os.environ.get("SONDER_SESSION_NUM_CTX") or "").strip()
    )
    if has_explicit_environment:
        base = default_requested()
        source = "environment"
    else:
        base = default_context()
        source = "kv-quantised-default" if _kv_cache_is_quantised() else "fp16-default"
    clamps = []
    chosen = base
    parameters = _parameter_billions(parameter_size)
    if parameters is not None and not has_explicit_environment:
        for floor, ceiling, reason in _PARAMETER_CONTEXT_BANDS:
            if parameters >= floor:
                if ceiling < chosen:
                    chosen = ceiling
                    clamps.append(reason)
                break
    advertised = parse_strict(model_context)
    if advertised is not None and advertised < chosen:
        chosen = advertised
        clamps.append("advertised-maximum")
    ceiling = native_max()
    if chosen > ceiling:
        chosen = ceiling
        clamps.append("native-maximum")
    if chosen < MIN_CONTEXT:
        chosen = MIN_CONTEXT
        clamps.append("minimum-window")
    return {
        "context": chosen,
        "base": base,
        "source": source,
        "parameter_billions": parameters,
        "advertised": advertised,
        "clamps": tuple(clamps),
    }


def auto_context(model_context=None, parameter_size=None) -> int:
    """Choose a model-aware native context when the caller did not pin one.

    Ollama's server default is process-wide, but model weights and KV-cache
    pressure are not. Keep more room for small local models and reserve a
    conservative share of the KV budget for larger models, then never exceed
    the selected model's advertised maximum. Operators can still override the
    result with ``SONDER_CONTEXT_SIZE`` or an explicit request value.
    """
    return auto_context_plan(model_context, parameter_size)["context"]


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
