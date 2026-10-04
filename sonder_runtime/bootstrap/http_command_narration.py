"""Small HTTP adapter helpers for narrating already-authorized commands.

The serving layer owns command parsing, authorization, and the concrete
handler.  This module only identifies long-running command shapes, builds a
deterministic acknowledgement from arguments already present at the host
boundary, and runs an already-authorized callable through the existing
``WorkRunner``/narration scope.
"""
from __future__ import annotations

from typing import Any, Callable

from sonder_runtime.bootstrap.http_work_narration import (
    prepare_ack,
    current,
    run_scoped,
    start_work,
)
from sonder_runtime.interfaces.http.work_runs import current_run_id
from sonder_runtime.application.chat.handoff_receipts import ChatWorkResult
from sonder_runtime.domain.cloud_access import has_legacy_error_prefix


# These are execution commands only.  Status, cancellation, inspection, and
# mode-selection commands must remain synchronous and retain their old output.
LONG_COMMANDS = frozenset({
    "work", "agent", "workbench_agent", "master", "master_orchestrate",
    "autopilot", "autopilot_start", "model_fanout", "fanout",
    "_model_fanout_authorized",
})
SYNCHRONOUS_SUFFIXES = frozenset({"status", "cancel", "capacity", "ask"})


def is_long_command(name: str, arguments: dict[str, Any] | None = None) -> bool:
    """Return whether a parsed, authorized command starts background work."""
    normalized = str(name or "").strip().lower().lstrip("/")
    if normalized not in LONG_COMMANDS:
        return False
    args = arguments if isinstance(arguments, dict) else {}
    action = str(args.get("action") or args.get("mode") or "").strip().lower()
    if normalized == "autopilot" and action not in {"run", "start", "plan"}:
        return False
    if normalized in {"master", "master_orchestrate"} and action not in {
        "inline", "master", "delegate", "delegated", "agents", "parallel", "fleet", "swarm", "fanout",
    }:
        return False
    if not any(args.get(key) for key in ("task", "objective", "prompt", "content")):
        return False
    return not any(action == suffix or action.endswith("_" + suffix)
                   for suffix in SYNCHRONOUS_SUFFIXES)


def command_ack(runtime: Any, name: str, arguments: dict[str, Any] | None = None,
                *, project: str = "", reason: str = "explicit execution command",
                run_id: str = "") -> str:
    """Build a deterministic acknowledgement from parsed host arguments."""
    args = arguments if isinstance(arguments, dict) else {}
    normalized = str(name or "").strip().lower().lstrip("/")
    mode = normalized
    if normalized in {"work", "agent", "workbench_agent"}:
        mode = "workbench"
    elif normalized in {"master", "master_orchestrate"}:
        mode = "workbench" if args.get("mode") in {"inline", "master"} else "fleet"
    elif normalized in {"autopilot", "autopilot_start"}:
        mode = "autopilot"
    elif normalized in {"model_fanout", "fanout", "_model_fanout_authorized"}:
        mode = "fanout"
    goal = (args.get("task") or args.get("objective") or args.get("prompt")
            or args.get("content") or "the requested work")
    agents = args.get("agents") or args.get("worker_cap")
    if normalized in {"master", "master_orchestrate"} and args.get("mode") in {"delegate", "delegated", "agents", "parallel"}:
        agents = agents or 3
    return prepare_ack(
        runtime, goal, mode, project=args.get("project") or project,
        reason=reason, run_id=run_id,
        agents=agents,
        worker_cap=args.get("worker_cap"), tier=args.get("tier", "auto"),
    )


def run_command(*, runner: Any, principal: str, call: Callable[[], object],
                classify: Callable[[object], tuple[str, str]], store: Any,
                tracker: Any, acknowledgement: str,
                thread_wrapper: Callable[[Callable[[], None]], Callable[[], None]] | None = None) -> Any:
    """Run an already-authorized command through the existing HTTP work fence.

    The caller remains responsible for parsing, auth gates, idempotency and
    response formatting.  The deferred starter is intentionally injected by
    the caller so ordinary HTTP requests can flush it after their response is
    assembled.
    """
    def narrated_call() -> object:
        return run_scoped(
            current_run_id(), store.link_run, acknowledgement,
            tracker.current_response_id() or "", call,
        )

    # ``start_work`` is the shared HTTP admission path.  Keep this adapter
    # thin so direct commands and natural work receive identical run receipts
    # and replay/cancel semantics.
    return start_work(
        runner, principal, narrated_call, acknowledgement, store,
        classify=classify, thread_wrapper=thread_wrapper,
    )


def dispatch(runtime, name, arguments, call, *, runner, principal, store, tracker,
             thread_wrapper=None):
    """Narrate concrete HTTP starts, preserving synchronous non-HTTP callers."""
    if name in {"master", "master_orchestrate"} and arguments.get("mode", "ask") == "ask":
        orchestrator = runtime.get("master_orchestrator") if isinstance(runtime, dict) else getattr(runtime, "master_orchestrator", None)
        if orchestrator and orchestrator.requests_fleet(arguments.get("task", "")):
            arguments = {**arguments, "mode": "fleet"}
    if current() is None or not is_long_command(name, arguments):
        return call()
    return run_command(
        runner=runner, principal=principal, store=store, tracker=tracker,
        acknowledgement=command_ack(runtime, name, arguments), call=call,
        classify=classify_result, thread_wrapper=thread_wrapper,
    )


def classify_result(result):
    if isinstance(result, ChatWorkResult):
        return result.status, result.text
    if not isinstance(result, str):
        return "unknown", str(result)
    text = result.strip()
    if text.lower().startswith("refused"):
        return "refused", result
    if has_legacy_error_prefix(text):
        return "failed", result
    return ("returned" if text else "unknown"), result


__all__ = [
    "LONG_COMMANDS", "SYNCHRONOUS_SUFFIXES", "command_ack",
    "is_long_command", "run_command",
]
