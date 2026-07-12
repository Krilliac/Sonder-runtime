"""Selectable native/virtual context sizing for Sonder Runtime.

Ollama/local models have a real native context window. Sonder Runtime can expose a
larger virtual budget by relying on summaries, retrieval, facts, and recent-turn
selection while clamping the actual Ollama num_ctx to a safe native limit.
"""
import os
import re


DEFAULT_CONTEXT = 8192
DEFAULT_NATIVE_MAX = 262144
DEFAULT_VIRTUAL_MAX = 1_000_000


def parse_size(value, default=DEFAULT_CONTEXT):
    if value is None:
        return int(default)
    if isinstance(value, (int, float)):
        return max(1, int(value))
    text = str(value).strip().lower().replace("_", "").replace(",", "")
    if not text:
        return int(default)
    m = re.match(r"^(\d+(?:\.\d+)?)(k|m)?$", text)
    if not m:
        return int(default)
    num = float(m.group(1))
    suffix = m.group(2)
    if suffix == "k":
        num *= 1000
    elif suffix == "m":
        num *= 1000000
    return max(1, int(num))


def native_max():
    return parse_size(os.environ.get("SONDER_NATIVE_CONTEXT_MAX"), DEFAULT_NATIVE_MAX)


def virtual_max():
    return parse_size(os.environ.get("SONDER_VIRTUAL_CONTEXT_MAX"), DEFAULT_VIRTUAL_MAX)


def default_requested():
    return parse_size(
        os.environ.get("SONDER_CONTEXT_SIZE")
        or os.environ.get("SONDER_SESSION_NUM_CTX"),
        DEFAULT_CONTEXT,
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
    p = policy(value)
    lines = [
        "context policy",
        "  requested: %(requested)s tokens" % p,
        "  ollama native num_ctx: %(native)s tokens" % p,
        "  mode: %(mode)s" % p,
        "  native max: %(native_max)s" % p,
        "  virtual max: %(virtual_max)s" % p,
    ]
    if p["virtual"]:
        lines.append(
            "  trick: prompts are kept within native num_ctx while summaries, "
            "retrieval, facts, and recent turns represent the larger virtual budget."
        )
    return "\n".join(lines)
