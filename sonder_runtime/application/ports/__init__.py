"""Explicit ports (SPEC-3 R-M4).

Model calls, repositories, clocks, process probes, filesystems,
executors, event sinks, and transactions are reached only through these
interfaces. Ports raise the domain error taxonomy, never transport or
driver exceptions.
"""
from .backup import BackupGateway, BackupPath, BackupResultView
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
from .task_state import ChecklistEventPort, TaskRepository
from .workflows import LoopRunner, WorkflowRepository

__all__ = [
    "AutomationRepository",
    "BackupGateway",
    "BackupPath",
    "BackupResultView",
    "ChecklistEventPort",
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
    "TaskRepository",
    "UnitOfWork",
    "LoopRunner",
    "WorkflowRepository",
]
