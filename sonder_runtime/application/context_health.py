"""Application service for the read-only context-health snapshot.

The service owns the snapshot contract and arithmetic, while the composition
root supplies session identity, persistence, context policy, and host paths.
It deliberately does not import the legacy server or a database adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class ContextIdentityPort(Protocol):
    """Resolve user-facing session/project selectors at the host boundary."""

    def session(self, value: str) -> str | None: ...

    def project(self, value: str) -> str | None: ...


class ContextHealthRepository(Protocol):
    """Read the bounded memory facts needed by the health surface."""

    def snapshot(self, session: str | None, project: str | None, limit: int) -> "ContextMemorySnapshot": ...


class ContextPolicyPort(Protocol):
    """Return the selected virtual/native context policy."""

    def policy(self, requested: Any) -> Mapping[str, Any]: ...


class ContextHealthMetrics(Protocol):
    """Host-provided token and meter policies."""

    def tokens(self, value: str) -> int: ...

    def tokens_from_chars(self, value: int) -> int: ...

    def bar(self, ratio: float) -> str: ...


@dataclass(frozen=True)
class ContextMemorySnapshot:
    turns: tuple[tuple[str, str], ...] = ()
    total_turns: int = 0
    title: str = ""
    summary: str = ""
    summarized_through: str = ""
    updated_ts: str = ""
    sessions: int = 0
    lessons: int = 0
    facts: int = 0
    preferences: int = 0
    interactions: int = 0
    outcomes: int = 0


@dataclass(frozen=True)
class ContextHealthSettings:
    requested_context: Any
    max_turns: int
    db_path: str
    state_home: str


class ContextHealthService:
    """Build the stable context-health response from injected ports."""

    def __init__(
        self,
        *,
        identity: ContextIdentityPort,
        repository: ContextHealthRepository,
        policy: ContextPolicyPort,
        metrics: ContextHealthMetrics,
        settings: ContextHealthSettings,
    ) -> None:
        self._identity = identity
        self._repository = repository
        self._policy = policy
        self._metrics = metrics
        self._settings = settings

    def snapshot(self, session: str = "", project: str = "") -> dict[str, Any]:
        session_id = self._identity.session(session)
        project_id = self._identity.project(project)
        memory = self._repository.snapshot(
            session_id, project_id, max(0, int(self._settings.max_turns)),
        )
        summary_tokens = self._metrics.tokens(memory.summary)
        live_chars = sum(len(task or "") + len(response or "") for task, response in memory.turns)
        live_tokens = self._metrics.tokens_from_chars(live_chars)
        estimated_tokens = summary_tokens + live_tokens

        values = self._policy.policy(self._settings.requested_context)
        context_limit = max(1, int(values["requested"] or 1))
        context_ratio = min(1.0, estimated_tokens / context_limit)
        if context_ratio >= 0.90:
            status = "hot"
        elif context_ratio >= 0.70:
            status = "warm"
        else:
            status = "healthy"
        live_turns = len(memory.turns)
        turn_ratio = min(1.0, live_turns / max(1, int(self._settings.max_turns or 1)))
        memory_items = memory.lessons + memory.facts + memory.preferences + memory.outcomes
        memory_ratio = min(1.0, memory_items / 1000.0)
        return {
            "session": session_id or "none",
            "project": project_id or "none",
            "title": memory.title,
            "status": status,
            "context_limit": context_limit,
            "native_context_limit": values["native"],
            "native_context_max": values["native_max"],
            "virtual_context_max": values["virtual_max"],
            "context_mode": values["mode"],
            "virtual_context": values["virtual"],
            "estimated_tokens": estimated_tokens,
            "context_percent": round(context_ratio * 100.0, 1),
            "context_bar": self._metrics.bar(context_ratio),
            "live_turns": live_turns,
            "max_live_turns": self._settings.max_turns,
            "total_turns": memory.total_turns,
            "turn_percent": round(turn_ratio * 100.0, 1),
            "turn_bar": self._metrics.bar(turn_ratio),
            "summary_tokens": summary_tokens,
            "live_tokens": live_tokens,
            "summary_chars": len(memory.summary),
            "summarized_through": memory.summarized_through,
            "updated_ts": memory.updated_ts,
            "sessions": memory.sessions,
            "lessons": memory.lessons,
            "facts": memory.facts,
            "preferences": memory.preferences,
            "interactions": memory.interactions,
            "outcomes": memory.outcomes,
            "memory_percent": round(memory_ratio * 100.0, 1),
            "memory_bar": self._metrics.bar(memory_ratio),
            "db_path": self._settings.db_path,
            "state_home": self._settings.state_home,
        }


__all__ = [
    "ContextHealthMetrics",
    "ContextHealthRepository",
    "ContextHealthService",
    "ContextHealthSettings",
    "ContextIdentityPort",
    "ContextMemorySnapshot",
    "ContextPolicyPort",
]
