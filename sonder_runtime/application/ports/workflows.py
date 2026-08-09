"""Ports for persistent saved workflows and bounded loop execution."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class WorkflowRepository(Protocol):
    def ensure(self) -> tuple[dict, str]: ...

    def save(
        self, name: str, actions: list[dict], description: str = ""
    ) -> tuple[dict, str]: ...

    def get(self, name: str) -> dict | None: ...

    def delete(self, name: str) -> tuple[bool, str]: ...

    def normalize_name(self, name: str) -> str: ...

    def format(self, workflows: dict) -> str: ...


class LoopRunner(Protocol):
    def run(
        self,
        actions: list[dict],
        dispatch: Callable[[dict], dict],
        *,
        max_iterations: int,
        stop_on_failure: bool,
        stop_on_success: bool,
        delay_seconds: float,
    ) -> dict: ...

    def format(self, result: dict) -> str: ...
