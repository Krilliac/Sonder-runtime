"""Lazy compatibility executor for startup preflight."""
from __future__ import annotations

from sonder_runtime.application.ports.preflight import (
    PreflightConfig,
    PreflightReport,
)


class LegacyPreflightExecutor:
    def run(
        self,
        config: PreflightConfig,
        *,
        check_ollama: bool = True,
        ollama_timeout: float = 5.0,
    ) -> PreflightReport:
        # Lazy so composing/importing the command surface performs no filesystem
        # probes, migration imports, or network setup.
        import sonder_runtime.adapters.preflight as implementation

        return implementation.run_preflight(
            config,
            check_ollama=check_ollama,
            ollama_timeout=ollama_timeout,
        )


__all__ = ["LegacyPreflightExecutor"]
