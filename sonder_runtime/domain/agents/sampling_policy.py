"""Pure, construction-time sampling policy for agent generators."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math

from ..common.errors import InvalidInput


def _number(raw: str, name: str, low: float, high: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidInput("%s must be a number" % name) from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise InvalidInput("%s must be between %s and %s" % (name, low, high))
    return value


def decision_temperature(env: Mapping[str, str]) -> float:
    return _number(env.get("SONDER_AGENT_TEMPERATURE", "0.1"), "SONDER_AGENT_TEMPERATURE", 0, 2)


@dataclass(frozen=True)
class SamplingPolicy:
    temperature: float
    sampling: tuple[tuple[str, float | int], ...]
    thinking_mode: str
    generation_kind: str

    def options(self, *, thinking_advertised: bool) -> dict:
        options = dict(self.sampling)
        if thinking_advertised:
            if self.thinking_mode == "on":
                options["think"] = True
            elif self.thinking_mode == "off" or self.generation_kind == "json":
                options["think"] = False
        return options


def sampling_policy(env: Mapping[str, str], *, generation_kind: str,
                    temperature: float) -> SamplingPolicy:
    """Parse only the knobs for this workload; caller owns provider gating."""
    if generation_kind not in ("decision", "json"):
        raise InvalidInput("unknown agent generation kind %r" % generation_kind)
    name = "SONDER_AGENT_DECISION_THINKING" if generation_kind == "decision" else "SONDER_AUTOPILOT_JSON_THINK"
    mode = env.get(name, "on" if generation_kind == "decision" else "auto").strip().lower()
    if mode not in ("on", "off", "auto"):
        raise InvalidInput("%s must be on, off or auto" % name)
    sampling = ()
    if generation_kind == "decision":
        temperature = decision_temperature(env)
        raw = env.get("SONDER_AGENT_SAMPLING", "").strip()
        if raw:
            parts = raw.split(",")
            if len(parts) != 3:
                raise InvalidInput("SONDER_AGENT_SAMPLING must be top_p,top_k,min_p")
            top_p = _number(parts[0], "top_p", 0, 1)
            top_k = _number(parts[1], "top_k", 0, 2147483647)
            min_p = _number(parts[2], "min_p", 0, 1)
            if not top_k.is_integer():
                raise InvalidInput("top_k must be an integer")
            sampling = (("top_p", top_p), ("top_k", int(top_k)), ("min_p", min_p))
    return SamplingPolicy(temperature, sampling, mode, generation_kind)
