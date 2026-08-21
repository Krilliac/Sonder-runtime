"""Bounded A2A discovery and remote-task identity projections.

This module describes what a host may publish or accept. It does not fetch
cards, call remote URLs, validate credentials, or grant a remote agent local
workspace access.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Iterable

from ...domain.common.errors import InvalidInput


MAX_SKILLS = 64
MAX_TEXT = 512
MAX_EXAMPLES = 8
MAX_CHAIN = 16


def _text(value, name: str, limit: int = MAX_TEXT) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise InvalidInput(f"{name} is invalid or exceeds its bound")
    return value.strip()


def _uri(value: str) -> str:
    value = _text(value, "url")
    prefix = "https://" if value.startswith("https://") else "http://" if value.startswith("http://") else ""
    authority = value[len(prefix):].split("/", 1)[0] if prefix else ""
    if not prefix or not authority or any(char.isspace() for char in value):
        raise InvalidInput("A2A URL must be an absolute HTTP(S) URI")
    return value


@dataclass(frozen=True, slots=True)
class A2ASkill:
    id: str
    name: str
    description: str
    input_modes: tuple[str, ...] = ("text",)
    output_modes: tuple[str, ...] = ("text",)
    examples: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "skill id", 128))
        object.__setattr__(self, "name", _text(self.name, "skill name", 128))
        object.__setattr__(self, "description", _text(self.description, "skill description"))
        for field_name in ("input_modes", "output_modes"):
            values = tuple(getattr(self, field_name))
            if not values or len(values) > 16 or any(not _text(item, field_name, 64) for item in values):
                raise InvalidInput(f"{field_name} are invalid")
            object.__setattr__(self, field_name, values)
        examples = tuple(self.examples)
        if len(examples) > MAX_EXAMPLES or any(not _text(item, "example", 256) for item in examples):
            raise InvalidInput("skill examples are invalid")
        object.__setattr__(self, "examples", examples)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "inputModes": list(self.input_modes),
            "outputModes": list(self.output_modes),
            "examples": list(self.examples),
        }


@dataclass(frozen=True, slots=True)
class A2AAgentCard:
    name: str
    description: str
    url: str
    version: str
    skills: tuple[A2ASkill, ...] = ()
    streaming: bool = False
    push_notifications: bool = False
    state_history: bool = False
    supported_interfaces: tuple[str, ...] = ("JSONRPC",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "agent name", 128))
        object.__setattr__(self, "description", _text(self.description, "agent description"))
        object.__setattr__(self, "url", _uri(self.url))
        object.__setattr__(self, "version", _text(self.version, "agent version", 64))
        skills = tuple(self.skills)
        if len(skills) > MAX_SKILLS or any(not isinstance(skill, A2ASkill) for skill in skills):
            raise InvalidInput("agent skills are invalid or exceed the bound")
        if len({skill.id for skill in skills}) != len(skills):
            raise InvalidInput("agent skill IDs must be unique")
        object.__setattr__(self, "skills", skills)
        interfaces = tuple(_text(item, "supported interface", 64) for item in self.supported_interfaces)
        if not interfaces:
            raise InvalidInput("at least one supported interface is required")
        object.__setattr__(self, "supported_interfaces", interfaces)
        for field_name in ("streaming", "push_notifications", "state_history"):
            if type(getattr(self, field_name)) is not bool:
                raise InvalidInput(f"{field_name} must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "supportedInterfaces": [
                {"url": self.url, "protocolBinding": item, "protocolVersion": "1.0"}
                for item in self.supported_interfaces
            ],
            "capabilities": {
                "streaming": self.streaming,
                "pushNotifications": self.push_notifications,
                "stateTransitionHistory": self.state_history,
            },
            "skills": [skill.to_dict() for skill in self.skills],
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


class A2ATaskState(str):
    WORKING = "TASK_STATE_WORKING"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    REJECTED = "TASK_STATE_REJECTED"


@dataclass(frozen=True, slots=True)
class A2ARemoteTaskRef:
    task_id: str
    context_id: str
    agent_card_digest: str
    state: str = A2ATaskState.WORKING
    delegation_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id", 128))
        object.__setattr__(self, "context_id", _text(self.context_id, "context_id", 128))
        object.__setattr__(self, "agent_card_digest", _text(self.agent_card_digest, "agent_card_digest", 128))
        allowed = {
            value for name, value in vars(A2ATaskState).items()
            if name.isupper() and isinstance(value, str)
        }
        if self.state not in allowed:
            raise InvalidInput("unsupported A2A task state")
        chain = tuple(_text(item, "delegation chain item", 128) for item in self.delegation_chain)
        if len(chain) > MAX_CHAIN:
            raise InvalidInput("delegation chain exceeds its bound")
        object.__setattr__(self, "delegation_chain", chain)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.task_id,
            "contextId": self.context_id,
            "state": self.state,
            "agentCardDigest": self.agent_card_digest,
            "delegationChain": list(self.delegation_chain),
        }


def card_from_registrations(
    name: str,
    description: str,
    url: str,
    registrations: Iterable[object],
    *,
    version: str = "1",
) -> A2AAgentCard:
    """Build a discovery card from existing provider-neutral registrations."""
    skills = []
    for registration in registrations:
        skill_name = _text(getattr(registration, "name", ""), "registration name", 128)
        role = _text(getattr(registration, "role", "agent"), "registration role", 128)
        capabilities = tuple(getattr(registration, "capabilities", ()))
        skills.append(A2ASkill(skill_name, skill_name, f"{role} agent: {', '.join(capabilities) or 'general'}"))
    return A2AAgentCard(name, description, url, version, tuple(skills))


__all__ = ["A2AAgentCard", "A2ARemoteTaskRef", "A2ASkill", "A2ATaskState", "card_from_registrations"]
