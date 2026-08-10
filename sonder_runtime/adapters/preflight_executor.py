"""Lazy preflight executor adapter (SPEC-5 WP11, replaces legacy)."""
from __future__ import annotations

from sonder_runtime.application.ports.preflight import (
    PreflightConfig,
    PreflightReport,
)


class PreflightExecutor:
    def run(
        self,
        config: PreflightConfig,
        *,
        check_ollama: bool = True,
        ollama_timeout: float = 5.0,
    ) -> PreflightReport:
        import sonder_runtime.adapters.preflight as implementation

        return implementation.run_preflight(
            config,
            check_ollama=check_ollama,
            ollama_timeout=ollama_timeout,
        )
