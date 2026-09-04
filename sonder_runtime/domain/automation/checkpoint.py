"""Agent session checkpoint data model.

Pure domain value object for checkpoint state.  The persistence layer
(``CheckpointStore``) lives in ``sonder_runtime.adapters.persistence``
to keep sqlite3 I/O out of the domain.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Checkpoint:
    session_id: str
    step_index: int
    status: str
    context: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    checkpoint_id: str = ""

    def __post_init__(self) -> None:
        if not self.checkpoint_id:
            self.checkpoint_id = "%s-%d-%.0f" % (
                self.session_id, self.step_index, self.created_at * 1000,
            )


__all__ = [
    "Checkpoint",
]
