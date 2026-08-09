"""Typed startup-preflight boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    required: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)

    @property
    def degraded(self) -> bool:
        return self.ok and any(not check.ok for check in self.checks)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "degraded": self.degraded,
            "checks": [check.as_dict() for check in self.checks],
        }


class PreflightStateConfig(Protocol):
    home: str
    workspace_roots: Sequence[str]
    minimum_free_disk_bytes: int


class PreflightOllamaConfig(Protocol):
    url: str


class PreflightConfig(Protocol):
    state: PreflightStateConfig
    ollama: PreflightOllamaConfig


class PreflightExecutor(Protocol):
    def run(
        self,
        config: PreflightConfig,
        *,
        check_ollama: bool = True,
        ollama_timeout: float = 5.0,
    ) -> PreflightReport: ...


# Keep repr/pickle/type paths stable for callers of the historical root module.
CheckResult.__module__ = "sonder_preflight"
PreflightReport.__module__ = "sonder_preflight"

__all__ = [
    "CheckResult",
    "PreflightConfig",
    "PreflightExecutor",
    "PreflightOllamaConfig",
    "PreflightReport",
    "PreflightStateConfig",
]
