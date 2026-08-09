"""Explicit ports (SPEC-3 R-M4).

Model calls, repositories, clocks, process probes, filesystems,
executors, event sinks, and transactions are reached only through these
interfaces. Ports raise the domain error taxonomy, never transport or
driver exceptions.
"""
from .clock import Clock
from .event_sink import EventSink
from .model_gateway import (
    Embedding,
    InferenceTelemetry,
    ModelGateway,
    ModelRequest,
    ModelResponse,
)
from .process_probe import ProbeResult, ProcessIdentity, ProcessProbe
from .repositories import (
    AutomationRepository,
    MemoryRepository,
    PolicyRepository,
    UnitOfWork,
)
from .tool_executor import ToolCall, ToolExecutor, ToolResult
from .workflows import LoopRunner, WorkflowRepository

__all__ = [
    "AutomationRepository",
    "Clock",
    "Embedding",
    "EventSink",
    "InferenceTelemetry",
    "MemoryRepository",
    "ModelGateway",
    "ModelRequest",
    "ModelResponse",
    "PolicyRepository",
    "ProbeResult",
    "ProcessIdentity",
    "ProcessProbe",
    "ToolCall",
    "ToolExecutor",
    "ToolResult",
    "UnitOfWork",
    "LoopRunner",
    "WorkflowRepository",
]
