"""Semantic recall gateway adapter."""
from __future__ import annotations

import importlib
import logging
import sqlite3
from typing import TYPE_CHECKING, Any, cast

from ..domain.common.errors import DependencyUnavailable

if TYPE_CHECKING:
    from .recall import RecallPage

logger = logging.getLogger(__name__)


class LegacyRecallGateway:
    def recall(self, connection: object, task: str, **options: Any) -> list[str]:
        logger.debug(f"recall gateway query options={list(options.keys())}")
        implementation = importlib.import_module("sonder_runtime.adapters.recall")
        try:
            result: object = implementation.recall(connection, task, **options)
            logger.debug(f"recall gateway returned {len(result) if isinstance(result, (list, tuple)) else 'non-list'} results")
            return cast(list[str], result)
        except (OSError, sqlite3.Error) as exc:
            logger.error(
                f"recall gateway storage operation failed, "
                f"error_type={type(exc).__name__!r}",
                exc_info=True,
            )
            logger.warning(
                f"recall gateway storage unavailable: {type(exc).__name__} — "
                f"semantic recall is degraded, queries will fail until storage recovers",
                exc_info=True,
            )
            raise DependencyUnavailable("semantic recall storage is unavailable") from exc

    def recall_page(self, connection: object, task: str, **options: Any) -> RecallPage:
        """Forward the additive provenance-aware page contract."""
        logger.debug(f"explained recall gateway query options={list(options.keys())}")
        implementation = importlib.import_module("sonder_runtime.adapters.recall")
        try:
            return cast("RecallPage", implementation.recall_page(connection, task, **options))
        except (OSError, sqlite3.Error) as exc:
            logger.error(
                f"explained recall gateway storage operation failed, "
                f"error_type={type(exc).__name__!r}",
                exc_info=True,
            )
            raise DependencyUnavailable("semantic recall storage is unavailable") from exc
