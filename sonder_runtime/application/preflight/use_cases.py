"""Startup-preflight orchestration independent of host I/O."""
from __future__ import annotations

import logging

from ..ports.preflight import PreflightConfig, PreflightExecutor, PreflightReport

logger = logging.getLogger(__name__)


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
        logger.info("preflight service running")
        logger.debug(f"PreflightService.run: check_ollama={check_ollama}, ollama_timeout={ollama_timeout}")
        report = self._executor.run(
            config,
            check_ollama=check_ollama,
            ollama_timeout=ollama_timeout,
        )
        if not getattr(report, 'passed', True):
            logger.error("preflight service startup checks did not pass, system may be degraded")
            logger.warning("preflight service reports startup checks did not pass")
        logger.info(f"preflight service completed, passed={getattr(report, 'passed', '?')}")
        logger.debug(f"PreflightService.run: complete, passed={getattr(report, 'passed', '?')}")
        return report


__all__ = ["PreflightService"]
