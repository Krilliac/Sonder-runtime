"""Port for semantic recall over the caller-owned memory connection."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol


Embedding = Sequence[float]
EmbedFunction = Callable[[str], Embedding | None]


class RecallGateway(Protocol):
    def recall(
        self,
        connection: Any,
        task: str,
        *,
        k: int = 2,
        embed_fn: EmbedFunction | None = None,
        min_sim: float | None = None,
        qv: Embedding | None = None,
        exclude_session: str | None = None,
        project: str | None = None,
        include_all_projects: bool = False,
        embedding_model: str | None = None,
        embedding_revision: str | None = None,
    ) -> list[str]: ...
