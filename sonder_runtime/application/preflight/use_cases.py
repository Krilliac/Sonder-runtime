"""Startup-preflight orchestration independent of host I/O."""
from __future__ import annotations

from ..ports.preflight import PreflightConfig, PreflightExecutor, PreflightReport


class PreflightService:
    def __init__(self, executor: PreflightExecutor) -> None:
        self._executor = executor

    def run(
        self,
        config: PreflightConfig,
        *,
        check_ollama: bool = True,
        ollama_timeout: float = 5.0,
    ) -> PreflightReport:
        return self._executor.run(
            config,
            check_ollama=check_ollama,
            ollama_timeout=ollama_timeout,
        )


__all__ = ["PreflightService"]
