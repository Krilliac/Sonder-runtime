"""Typed tool descriptors and effects (SPEC-5 §15).

Pure domain types — no I/O, no subprocess, no container runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class ToolEffect(Enum):
    READ_FILES = auto()
    WRITE_FILES = auto()
    DELETE_FILES = auto()
    EXECUTE = auto()
    NETWORK = auto()
    GIT_WRITE = auto()
    PACKAGE_INSTALL = auto()
    SELFMOD = auto()


class ExecutionClass(Enum):
    PURE = auto()
    CONTAINER = auto()
    HOST = auto()


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    input_schema: dict = field(default_factory=dict)
    effects: frozenset[ToolEffect] = field(default_factory=frozenset)
    execution_class: ExecutionClass = ExecutionClass.PURE


@dataclass(frozen=True)
class ToolCall:
    tool_name: str
    arguments: dict = field(default_factory=dict)
    call_id: str = ""


@dataclass(frozen=True)
class ToolResult:
    tool_name: str
    output: str = ""
    error: str = ""
    success: bool = True
    duration_ms: int = 0
