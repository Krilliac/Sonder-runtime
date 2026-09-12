"""Typed [spanda] configuration — Exact-Match R_sc epistemic uncertainty.

Default OFF. Enabling multi-samples chat completions; never weakens auth.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpandaConfig:
    enabled: bool = False
    k: int = 3
    threshold: float = 0.35
    block: bool = False
    alpha: float = 0.5
    # Sampling temperature floor when Spanda is active (must be > 0).
    sample_temperature: float = 0.2


def spanda_errors(config) -> list[str]:
    section = config.spanda
    errors: list[str] = []
    if type(section.enabled) is not bool:
        return ["[spanda].enabled must be a boolean"]
    if type(section.block) is not bool:
        errors.append("[spanda].block must be a boolean")
    if type(section.k) is not int or not 2 <= section.k <= 16:
        errors.append("[spanda].k must be an integer in 2..16")
    if type(section.threshold) is not float and type(section.threshold) is not int:
        errors.append("[spanda].threshold must be a number")
    else:
        thr = float(section.threshold)
        if not 0.0 <= thr <= 1.0:
            errors.append("[spanda].threshold must be within 0.0..1.0")
    if type(section.alpha) is not float and type(section.alpha) is not int:
        errors.append("[spanda].alpha must be a number")
    else:
        alpha = float(section.alpha)
        if not 0.0 <= alpha <= 1.0:
            errors.append("[spanda].alpha must be within 0.0..1.0")
    if (
        type(section.sample_temperature) is not float
        and type(section.sample_temperature) is not int
    ):
        errors.append("[spanda].sample_temperature must be a number")
    else:
        temp = float(section.sample_temperature)
        if not 0.0 < temp <= 2.0:
            errors.append("[spanda].sample_temperature must be within (0.0, 2.0]")
    return errors


__all__ = ["SpandaConfig", "spanda_errors"]
