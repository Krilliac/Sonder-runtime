"""Ports and value types of the security-hypothesis eval (no behaviour)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ...domain.security_eval.context_select import ContextBundle
from ...domain.security_eval.records import LineRange

TOKEN_MEASURED = "measured"
TOKEN_UNVERIFIED = "gateway_unverified"
SERVED_MODEL_SOURCE = "gateway_resolved"


@dataclass(frozen=True)
class SourceWindow:
    """A line window of one repo file: ``text`` holds lines ``start..end`` (1-based, inclusive)."""

    rel: str
    start: int
    end: int
    text: str
    total_lines: int


@dataclass(frozen=True)
class ModelCall:
    """One model reply with the model the gateway says served it and the token evidence."""

    text: str
    requested_model: str
    served_model: str
    served_model_source: str
    tokens_in: int | None
    tokens_out: int | None
    token_source: str
    duration_ms: int


class Repo(Protocol):
    def read_window(self, rel: str, lines: LineRange, pad: int) -> SourceWindow: ...

    def read_chunks(self, rel: str, max_chars: int) -> tuple[SourceWindow, ...]: ...

    def retrieve(self, rel: str, lines: LineRange) -> ContextBundle: ...


RepoProvider = Callable[[str], Repo]


class VerdictModel(Protocol):
    def preflight(self) -> None:
        """Refuse a route that may leave the machine, before any request is sent."""

    def ask(self, system: str, user: str) -> ModelCall: ...


class ResultStore(Protocol):
    def read_rows(self) -> list[dict]: ...

    def append(self, row: dict) -> None: ...

    def write_report(self, report: dict, markdown: str) -> None: ...


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
