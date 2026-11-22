"""Lazy preflight executor adapter (SPEC-5 WP11, replaces legacy)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from sonder_runtime.application.ports.preflight import (
    PreflightConfig,
    PreflightReport,
)

if TYPE_CHECKING:
    from sonder_runtime.platform.config import SonderConfig

logger = logging.getLogger(__name__)


class PreflightExecutor:
    def run(
        self,
        config: PreflightConfig,
        *,
        check_ollama: bool = True,
        ollama_timeout: float = 5.0,
    ) -> PreflightReport:
        logger.info(f"preflight check starting, check_ollama={check_ollama}")
        logger.debug(f"preflight run: check_ollama={check_ollama}, ollama_timeout={ollama_timeout}")
        import sonder_runtime.adapters.preflight as implementation

        report = implementation.run_preflight(
            cast("SonderConfig", config),
            check_ollama=check_ollama,
            ollama_timeout=ollama_timeout,
        )
        if not getattr(report, 'ok', True):
            logger.warning(f"preflight check did not pass, runtime may be in a degraded state")
        logger.info(f"preflight check completed, ok={getattr(report, 'ok', '?')}")
        logger.debug(f"preflight complete: ok={getattr(report, 'ok', '?')}")
        return report
