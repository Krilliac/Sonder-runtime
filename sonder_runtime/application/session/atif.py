"""ATIF (Agent Trajectory Interchange Format) projection of Sonder sessions.

Harbor / Terminal-Bench tooling reads agent runs as ATIF ``trajectory.json``
documents (``ATIF-v1.7``: RFC ``harbor-framework/harbor`` rfcs/0001).  This
module projects Sonder's two session stores into that shape:

* ``session_events_to_atif`` -- the durable, append-only session event stream
  (``model.requested`` / ``provider.*`` / ``tool.call`` / ``tool.result`` /
  ``model.response`` / ``subagent.*`` ...), read through the same bounded,
  redacted ``SessionQueryEngine.export_events`` envelope the default export
  uses.  Privacy-retention markers therefore apply before this projection
  ever sees a payload.
* ``interaction_turns_to_atif`` -- the legacy remembered-conversation
  interactions (``task`` / ``response`` / token counts) behind the
  ``session_export`` MCP tool.

It is an *additional* format: the existing exports are untouched.  Every
string in the finished document passes a final fail-closed redaction pass
(the domain credential patterns, then the export redactor), so a secret
shape that slipped past capture-time redaction is still scrubbed.  The pass
is deterministic, so ``tool_call_id`` / ``source_call_id`` linkage and
subagent ``trajectory_id`` references stay intact.

The module is pure: no I/O, no clock, no configuration.  Callers provide the
agent identity and, optionally, a loader for delegated child sessions.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any

from ...domain.security import redaction as _redaction
from .provider_attempts import _response_evidence
from .query_export import DefaultExportRedactor, SessionEventRecord


ATIF_SCHEMA_VERSION = "ATIF-v1.7"
ATIF_FORMAT = "atif"
DEFAULT_AGENT_NAME = "sonder"
MAX_SUBAGENT_DEPTH = 4
# Total child sessions embedded in one document (all depths): each costs a
# bounded export read, so breadth is capped as well as depth.
MAX_EMBEDDED_SUBAGENTS = 16
_MAX_WALK_DEPTH = 64

_USER_EVENTS = frozenset({"user.message", "message.received"})
_AGENT_TEXT_EVENTS = frozenset({"model.response", "message.emitted"})
_TOOL_CALL_EVENTS = frozenset({"tool.call", "tool.requested"})
_TOOL_RESULT_EVENTS = frozenset({"tool.result", "tool.completed", "tool.failed"})
_SUBAGENT_OPEN = frozenset({"subagent.spawned", "subagent.started"})
_SUBAGENT_CLOSE = frozenset({"subagent.completed", "subagent.failed"})
_ERROR_EVENTS = frozenset({"error.raised"})
_BOOKKEEPING = frozenset({"session.retention.applied"})
_PRIVACY_PLACEHOLDER = _USER_EVENTS | _AGENT_TEXT_EVENTS | _TOOL_CALL_EVENTS | _TOOL_RESULT_EVENTS


class AtifExportError(ValueError):
    """The session cannot be represented as a valid ATIF trajectory."""


@dataclass(frozen=True, slots=True)
class AtifAgent:
    """Identity of the agent system that produced the trajectory."""

    name: str = DEFAULT_AGENT_NAME
    version: str = "unknown"
    model_name: str | None = None

    def to_dict(self, model_name: str | None = None) -> dict[str, object]:
        agent: dict[str, object] = {"name": self.name, "version": self.version}
        chosen = self.model_name or model_name
        if chosen:
            agent["model_name"] = chosen
        return agent


SubagentLoader = Callable[[str], "tuple[str, Sequence[SessionEventRecord]] | None"]


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

_EXPORT_REDACTOR = DefaultExportRedactor()
# Fixed enum/version values carry no content and must stay spec-exact; any
# other value under these keys (e.g. inside ``extra``) is redacted as usual.
_VERBATIM_VALUES = {
    "schema_version": frozenset("ATIF-v1.%d" % minor for minor in range(0, 9)),
    "source": frozenset({"system", "user", "agent"}),
}


def _redact_text(text: str) -> str:
    """Domain credential patterns, then the export redactor; fail closed."""
    try:
        out = _EXPORT_REDACTOR.redact(_redaction.redact_text(text))
    except Exception:
        return _redaction.REDACTION_FAILED
    return out if isinstance(out, str) else _redaction.REDACTION_FAILED


def _redact_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    """Key-aware walk: ``{"password": "x"}`` has no shape a pattern can see."""
    walked = _redaction.redact_structure(dict(arguments), _redact_text, sensitive_keys=True)
    return walked if isinstance(walked, dict) else {"arguments": _redaction.REDACTED}


def redact_atif(document: Any, _depth: int = 0) -> Any:
    """Redact every string in an ATIF document, preserving its shape.

    Tool-call ``arguments`` additionally get the key-aware structure walk.
    Beyond the depth bound a subtree is replaced by ``[REDACTED]`` rather than
    returned unexamined.
    """
    if _depth > _MAX_WALK_DEPTH:
        return _redaction.REDACTED
    if isinstance(document, str):
        return _redact_text(document)
    if isinstance(document, Mapping):
        out: dict[str, object] = {}
        for key, value in document.items():
            if isinstance(value, str) and value in _VERBATIM_VALUES.get(key, ()):
                out[key] = value
            elif key == "arguments" and isinstance(value, Mapping):
                out[key] = _redact_arguments(value)
            else:
                out[key] = redact_atif(value, _depth + 1)
        return out
    if isinstance(document, (list, tuple)):
        return [redact_atif(item, _depth + 1) for item in document]
    return document


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _iso(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _sqlite_ts(value: object) -> str | None:
    """SQLite ``CURRENT_TIMESTAMP`` (UTC, ``YYYY-MM-DD HH:MM:SS``) -> ISO 8601."""
    if not isinstance(value, str) or not value:
        return None
    candidate = value.strip().replace(" ", "T", 1)
    if "+" not in candidate[10:] and not candidate.endswith("Z"):
        candidate += "Z"
    return _iso(candidate)


def _duration_ms(start: object, end: object) -> int | None:
    a, b = _iso(start), _iso(end)
    if a is None or b is None:
        return None
    try:
        delta = (datetime.fromisoformat(b.replace("Z", "+00:00"))
                 - datetime.fromisoformat(a.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None
    millis = int(delta.total_seconds() * 1000)
    return millis if millis >= 0 else None


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


# The export redactor rewrites ``"password":"x"`` to ``"password":[REDACTED]``
# (the quoted value is consumed), which is no longer JSON.  Re-quote exactly
# that marker in a value position so the arguments stay an object.
_BARE_REDACTION = re.compile(r'(?<=[:\[,])(\s*)\[REDACTED\](?=\s*[,}\]])')


def _loads(raw: str) -> tuple[object, bool]:
    try:
        return json.loads(raw), False
    except (TypeError, ValueError):
        pass
    repaired = _BARE_REDACTION.sub(r'\1"[REDACTED]"', raw)
    if repaired == raw:
        raise ValueError("not JSON")
    return json.loads(repaired), True


def _parse_arguments(raw: object) -> tuple[dict[str, object], dict[str, object]]:
    """Return (arguments object, extra) for a recorded tool-call payload."""
    if isinstance(raw, Mapping):
        return dict(raw), {}
    if not isinstance(raw, str):
        return {}, {}
    try:
        parsed, repaired = _loads(raw)
    except (TypeError, ValueError):
        # Not JSON even after re-quoting redaction markers: keep it, labelled.
        return {"arguments_text": raw}, {"arguments_unparsed": True}
    extra: dict[str, object] = {"arguments_redaction_requoted": True} if repaired else {}
    if isinstance(parsed, dict):
        return parsed, extra
    return {"value": parsed}, extra


def _result_content(payload: Mapping[str, object]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if "result" in payload:
        try:
            return json.dumps(payload["result"], ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(payload["result"])
    ref = _text(payload, "result_ref")
    return ref or ""


def _privacy_redacted(payload: Mapping[str, object]) -> str | None:
    """The privacy class when a retention marker replaced this payload."""
    if payload.get("redacted") is True and isinstance(payload.get("privacy_class"), str):
        return str(payload["privacy_class"])
    return None


# --------------------------------------------------------------------------
# Durable session events -> ATIF
# --------------------------------------------------------------------------

@dataclass(slots=True)
class _Attempt:
    attempt_id: str
    provider: str | None
    started: str | None
    model: str | None = None
    finished: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error_code: str | None = None
    responded: bool = False


@dataclass(slots=True)
class _EmbedBudget:
    remaining: int = MAX_EMBEDDED_SUBAGENTS


class _Builder:
    def __init__(self, session_id: str, agent: AtifAgent, loader: SubagentLoader | None,
                 depth: int, visited: frozenset[str], budget: _EmbedBudget | None = None) -> None:
        self.session_id = session_id
        self.budget = budget if budget is not None else _EmbedBudget()
        self.agent = agent
        self.loader = loader
        self.depth = depth
        self.visited = visited | {session_id}
        self.steps: list[dict[str, Any]] = []
        self.requests: dict[str, dict[str, object]] = {}
        self.attempts: dict[str, list[_Attempt]] = {}
        self.attempt_index: dict[str, _Attempt] = {}
        self.models: Counter[str] = Counter()
        self.last_system: str | None = None
        self.tool_step: dict[str, Any] | None = None
        self.call_steps: dict[str, dict[str, Any]] = {}
        self.subagent_steps: dict[str, dict[str, Any]] = {}
        self.subagents: list[dict[str, Any]] = []
        self.unmapped: Counter[str] = Counter()

    # -- step helpers ------------------------------------------------------
    def _add(self, step: dict[str, Any]) -> dict[str, Any]:
        step["step_id"] = len(self.steps) + 1
        self.steps.append(step)
        return step

    @staticmethod
    def _base(record: SessionEventRecord, source: str, message: str) -> dict[str, Any]:
        step: dict[str, Any] = {"source": source, "message": message}
        timestamp = _iso(record.occurred_at_utc)
        if timestamp is not None:
            step["timestamp"] = timestamp
        step["extra"] = {"sonder_sequence": record.sequence,
                         "sonder_event_type": record.event_type}
        return step

    def _close_tool_step(self) -> None:
        self.tool_step = None

    # -- event handlers ----------------------------------------------------
    def feed(self, record: SessionEventRecord) -> None:
        payload = record.payload if isinstance(record.payload, Mapping) else {}
        kind = record.event_type
        privacy_class = _privacy_redacted(payload)
        if privacy_class is not None and kind not in _PRIVACY_PLACEHOLDER:
            self.unmapped["privacy_redacted:" + kind] += 1
            return
        if privacy_class is not None:
            # Retention replaced the payload: keep a placeholder step where the
            # content was, never the content (and never a guessed linkage).
            self._close_tool_step()
            source = "user" if kind in _USER_EVENTS else "agent"
            step = self._base(record, source, _redaction.REDACTED)
            step["extra"]["privacy_class"] = privacy_class
            self._add(step)
            return
        if kind in _TOOL_CALL_EVENTS:
            self._tool_call(record, payload)
            return
        if kind in _TOOL_RESULT_EVENTS:
            self._tool_result(record, payload)
            return
        self._close_tool_step()
        if kind in _USER_EVENTS:
            content = payload.get("content")
            if isinstance(content, str):
                step = self._base(record, "user", content)
                turn = _text(payload, "turn_id")
                if turn:
                    step["extra"]["turn_id"] = turn
                self._add(step)
            else:
                self.unmapped[kind] += 1
        elif kind == "model.requested":
            self._model_requested(record, payload)
        elif kind == "provider.requested":
            self._provider_requested(record, payload)
        elif kind in {"provider.responded", "provider.failed"}:
            self._provider_finished(record, payload)
        elif kind in _AGENT_TEXT_EVENTS:
            content = payload.get("content")
            if isinstance(content, str):
                self._agent_turn(record, payload, content, None)
            else:
                self.unmapped[kind] += 1
        elif kind == "model.failed":
            self._agent_turn(record, payload, "", _text(payload, "error_code") or "MODEL_FAILED")
        elif kind in _SUBAGENT_OPEN:
            self._subagent_open(record, payload)
        elif kind in _SUBAGENT_CLOSE:
            self._subagent_close(record, payload)
        elif kind in _ERROR_EVENTS:
            code = _text(payload, "error_code") or "ERROR"
            step = self._base(record, "system", "error: %s" % code)
            step["extra"].update({"status": "error", "error_code": code})
            self._add(step)
        elif kind not in _BOOKKEEPING:
            self.unmapped[kind] += 1

    def _model_requested(self, record: SessionEventRecord, payload: Mapping[str, object]) -> None:
        request_id = _text(payload, "request_id")
        if request_id:
            self.requests[request_id] = {
                "tier": _text(payload, "tier"),
                "model": _text(payload, "model"),
                "turn_id": _text(payload, "turn_id"),
                "started": record.occurred_at_utc,
            }
        system = payload.get("system")
        if isinstance(system, str) and system.strip() and system != self.last_system:
            self.last_system = system
            step = self._base(record, "system", system)
            if request_id:
                step["extra"]["request_id"] = request_id
            self._add(step)

    def _provider_requested(self, record: SessionEventRecord, payload: Mapping[str, object]) -> None:
        attempt_id = _text(payload, "attempt_id")
        request_id = _text(payload, "request_id")
        if not attempt_id or not request_id:
            self.unmapped[record.event_type] += 1
            return
        body = payload.get("payload")
        model = _text(body, "model") if isinstance(body, Mapping) else None
        attempt = _Attempt(attempt_id, _text(payload, "provider"), record.occurred_at_utc, model)
        self.attempts.setdefault(request_id, []).append(attempt)
        self.attempt_index[attempt_id] = attempt

    def _provider_finished(self, record: SessionEventRecord, payload: Mapping[str, object]) -> None:
        attempt = self.attempt_index.get(_text(payload, "attempt_id") or "")
        if attempt is None:
            self.unmapped[record.event_type] += 1
            return
        attempt.finished = record.occurred_at_utc
        if record.event_type == "provider.failed":
            attempt.error_code = _text(payload, "error_code") or "PROVIDER_FAILED"
            return
        attempt.responded = True
        model, prompt, completion = _response_evidence(payload.get("response"))
        if model:
            attempt.model = model
        attempt.prompt_tokens, attempt.completion_tokens = prompt, completion

    def _agent_turn(self, record: SessionEventRecord, payload: Mapping[str, object],
                    message: str, error_code: str | None) -> None:
        step = self._base(record, "agent", message)
        extra = step["extra"]
        request_id = _text(payload, "request_id")
        turn_id = _text(payload, "turn_id")
        if turn_id:
            extra["turn_id"] = turn_id
        if error_code is not None:
            extra.update({"status": "failed", "error_code": error_code})
        request = self.requests.get(request_id or "", {})
        attempts = self.attempts.get(request_id or "", [])
        if request_id:
            extra["request_id"] = request_id
        if request.get("tier"):
            extra["tier"] = request["tier"]
        responded = [a for a in attempts if a.responded]
        failed = [a for a in attempts if a.error_code]
        model = next((a.model for a in reversed(responded) if a.model), None)
        model = model or next((a.model for a in reversed(attempts) if a.model), None)
        model = model or (request.get("model") if isinstance(request.get("model"), str) else None)
        if model:
            step["model_name"] = model
            self.models[model] += 1
        if responded:
            # One ATIF step per Sonder model request; a request retried across
            # provider attempts aggregates them, which llm_call_count declares.
            step["llm_call_count"] = len(responded)
            metrics: dict[str, Any] = {}
            prompts = [a.prompt_tokens for a in responded if a.prompt_tokens is not None]
            completions = [a.completion_tokens for a in responded if a.completion_tokens is not None]
            if prompts:
                metrics["prompt_tokens"] = sum(prompts)
            if completions:
                metrics["completion_tokens"] = sum(completions)
            metric_extra: dict[str, object] = {}
            durations = [d for d in (_duration_ms(a.started, a.finished) for a in responded) if d is not None]
            if durations:
                metric_extra["provider_duration_ms"] = sum(durations)
            providers = sorted({a.provider for a in responded if a.provider})
            if providers:
                metric_extra["providers"] = providers
            if metric_extra:
                metrics["extra"] = metric_extra
            if metrics:
                step["metrics"] = metrics
        if failed:
            extra["provider_failures"] = [
                {"attempt_id": a.attempt_id, "error_code": a.error_code} for a in failed
            ]
        turn_ms = _duration_ms(request.get("started"), record.occurred_at_utc)
        if turn_ms is not None:
            extra["request_duration_ms"] = turn_ms
        self._add(step)

    def _tool_call(self, record: SessionEventRecord, payload: Mapping[str, object]) -> None:
        call_id = _text(payload, "call_id")
        name = _text(payload, "name") or _text(payload, "tool")
        if not call_id or not name:
            self.unmapped[record.event_type] += 1
            return
        if call_id in self.call_steps:
            # A duplicate call id cannot be linked unambiguously; keep the first.
            self.unmapped[record.event_type + ".duplicate"] += 1
            return
        if self.tool_step is None:
            step = self._base(record, "agent", "")
            step["extra"] = {"sonder_sequence": record.sequence,
                             "sonder_event_type": "tool.dispatch"}
            turn_id = _text(payload, "turn_id")
            if turn_id:
                step["extra"]["turn_id"] = turn_id
            step["tool_calls"] = []
            self.tool_step = self._add(step)
        arguments, arg_extra = _parse_arguments(payload.get("content", payload.get("arguments")))
        call: dict[str, Any] = {"tool_call_id": call_id, "function_name": name,
                                "arguments": arguments}
        extra = dict(arg_extra)
        ref = _text(payload, "arguments_ref")
        if ref:
            extra["arguments_ref"] = ref
        extra["sonder_sequence"] = record.sequence
        call["extra"] = extra
        self.tool_step["tool_calls"].append(call)
        self.call_steps[call_id] = self.tool_step

    def _tool_result(self, record: SessionEventRecord, payload: Mapping[str, object]) -> None:
        call_id = _text(payload, "call_id")
        failed = record.event_type == "tool.failed"
        extra: dict[str, object] = {"sonder_sequence": record.sequence,
                                    "sonder_event_type": record.event_type}
        if failed:
            code = _text(payload, "error_code") or "TOOL_FAILED"
            content = "ERROR: %s" % code
            extra.update({"status": "failed", "error_code": code})
        else:
            content = _result_content(payload)
            extra["status"] = "completed"
        duration = payload.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            extra["duration_ms"] = duration
        step = self.call_steps.get(call_id or "")
        result: dict[str, Any] = {"content": content, "extra": extra}
        if step is None:
            # No recorded action to link to: keep the observation, unlinked.
            self._close_tool_step()
            if call_id:
                extra["unlinked_call_id"] = call_id
            orphan = self._base(record, "agent", "")
            orphan["llm_call_count"] = 0
            orphan["observation"] = {"results": [result]}
            self._add(orphan)
            return
        result = {"source_call_id": call_id, **result}
        step.setdefault("observation", {"results": []})["results"].append(result)

    def _subagent_open(self, record: SessionEventRecord, payload: Mapping[str, object]) -> None:
        subagent_id = _text(payload, "subagent_id") or _text(payload, "child_session_id")
        if not subagent_id:
            self.unmapped[record.event_type] += 1
            return
        if subagent_id in self.subagent_steps:
            return
        call_id = "subagent-%s" % subagent_id
        arguments: dict[str, object] = {"subagent_id": subagent_id}
        role = _text(payload, "role")
        if role:
            arguments["role"] = role
        step = self._base(record, "agent", "")
        step["llm_call_count"] = 0
        step["tool_calls"] = [{"tool_call_id": call_id, "function_name": "delegate_subagent",
                               "arguments": arguments}]
        result: dict[str, Any] = {"source_call_id": call_id, "content": "spawned",
                                  "extra": {"status": "spawned"}}
        embedded = self._embed(subagent_id, result["extra"])
        if embedded is not None:
            result["subagent_trajectory_ref"] = [{
                "trajectory_id": embedded["trajectory_id"],
                "session_id": embedded.get("session_id", embedded["trajectory_id"]),
            }]
        else:
            result["extra"]["subagent_resolved"] = False
        step["observation"] = {"results": [result]}
        self.subagent_steps[subagent_id] = self._add(step)

    def _subagent_close(self, record: SessionEventRecord, payload: Mapping[str, object]) -> None:
        subagent_id = _text(payload, "subagent_id") or _text(payload, "child_session_id")
        step = self.subagent_steps.get(subagent_id or "")
        if step is None:
            self.unmapped[record.event_type] += 1
            return
        result = step["observation"]["results"][0]
        if record.event_type == "subagent.failed":
            code = _text(payload, "error_code") or "SUBAGENT_FAILED"
            result["content"] = "failed: %s" % code
            result["extra"].update({"status": "failed", "error_code": code})
        else:
            result["content"] = "completed"
            result["extra"]["status"] = "completed"
            ref = _text(payload, "result_ref")
            if ref:
                result["extra"]["result_ref"] = ref

    def _embed(self, subagent_id: str, extra: dict[str, object]) -> dict[str, Any] | None:
        if self.loader is None or self.depth >= MAX_SUBAGENT_DEPTH:
            return None
        if self.budget.remaining <= 0:
            extra["subagent_budget_exhausted"] = True
            return None
        self.budget.remaining -= 1
        loaded = self.loader(subagent_id)
        if loaded is None:
            return None
        child_session, child_records = loaded
        if child_session in self.visited or any(
            item["trajectory_id"] == child_session for item in self.subagents
        ):
            return None
        builder = _Builder(child_session, self.agent, self.loader, self.depth + 1, self.visited,
                           self.budget)
        for child_record in child_records:
            builder.feed(child_record)
        if not builder.steps:
            return None
        child = builder.document(trajectory_id=child_session)
        self.subagents.append(child)
        return child

    # -- assembly ----------------------------------------------------------
    def document(self, *, trajectory_id: str,
                 extra: Mapping[str, object] | None = None) -> dict[str, Any]:
        agent = self.agent
        model = self.models.most_common(1)[0][0] if self.models else None
        doc: dict[str, Any] = {
            "schema_version": ATIF_SCHEMA_VERSION,
            "session_id": self.session_id,
            "trajectory_id": trajectory_id,
            "agent": agent.to_dict(model),
            "steps": self.steps,
        }
        doc["final_metrics"] = _final_metrics(self.steps)
        if self.subagents:
            doc["subagent_trajectories"] = self.subagents
        root_extra: dict[str, object] = {"sonder_source": "durable_session_events"}
        if self.unmapped:
            root_extra["unmapped_event_counts"] = dict(sorted(self.unmapped.items()))
        if extra:
            root_extra.update(extra)
        doc["extra"] = root_extra
        return doc


def _final_metrics(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    prompts, completions, calls = [], [], []
    for step in steps:
        metrics = step.get("metrics") or {}
        if isinstance(metrics.get("prompt_tokens"), int):
            prompts.append(metrics["prompt_tokens"])
        if isinstance(metrics.get("completion_tokens"), int):
            completions.append(metrics["completion_tokens"])
        if isinstance(step.get("llm_call_count"), int):
            calls.append(step["llm_call_count"])
    final: dict[str, Any] = {"total_steps": len(steps)}
    if prompts:
        final["total_prompt_tokens"] = sum(prompts)
    if completions:
        final["total_completion_tokens"] = sum(completions)
    if calls:
        final["extra"] = {"llm_call_count": sum(calls),
                          "steps_with_llm_call_count": len(calls)}
    return final


def session_events_to_atif(
    records: Sequence[SessionEventRecord], *, session_id: str,
    agent: AtifAgent | None = None, load_subagent: SubagentLoader | None = None,
    extra: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Project redacted durable session records into one ATIF document.

    ``load_subagent(subagent_id)`` may return ``(child_session_id, records)``
    for a delegated child session; it is then embedded in
    ``subagent_trajectories`` and referenced by ``trajectory_id`` from the
    delegating step's observation.  Without a loader (or when the child is
    not found) the delegation step is kept and marked unresolved.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise AtifExportError("session_id must be non-empty")
    builder = _Builder(session_id, agent or AtifAgent(), load_subagent, 0, frozenset())
    for record in records:
        builder.feed(record)
    if not builder.steps:
        raise AtifExportError("session has no steps to export")
    document = builder.document(trajectory_id=session_id, extra=extra)
    return redact_atif(document)


# --------------------------------------------------------------------------
# Legacy interaction turns -> ATIF
# --------------------------------------------------------------------------

def interaction_turns_to_atif(
    turns: Sequence[Mapping[str, object]], *, session_id: str,
    agent: AtifAgent | None = None, extra: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Project remembered-conversation turns (one user + one agent step each).

    Each turn is an interactions row: ``id``, ``task``, ``response``, and
    optionally ``tier``, ``ts``, ``tokens_in``, ``tokens_out``,
    ``token_source``.  The interactions store records no tool calls, so none
    are invented.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise AtifExportError("session_id must be non-empty")
    steps: list[dict[str, Any]] = []
    for turn in turns:
        timestamp = _sqlite_ts(turn.get("ts"))
        interaction_id = turn.get("id")
        user: dict[str, Any] = {"step_id": len(steps) + 1, "source": "user",
                                "message": str(turn.get("task") or "")}
        if timestamp:
            user["timestamp"] = timestamp
        if interaction_id:
            user["extra"] = {"interaction_id": str(interaction_id)}
        steps.append(user)
        reply: dict[str, Any] = {"step_id": len(steps) + 1, "source": "agent",
                                 "message": str(turn.get("response") or "")}
        if timestamp:
            reply["timestamp"] = timestamp
        metrics: dict[str, Any] = {}
        prompt, completion = _count(turn.get("tokens_in")), _count(turn.get("tokens_out"))
        if prompt is not None:
            metrics["prompt_tokens"] = prompt
        if completion is not None:
            metrics["completion_tokens"] = completion
        if metrics:
            source = turn.get("token_source")
            if isinstance(source, str) and source:
                metrics["extra"] = {"token_source": source}
            reply["metrics"] = metrics
        reply_extra: dict[str, object] = {}
        if interaction_id:
            reply_extra["interaction_id"] = str(interaction_id)
        if isinstance(turn.get("tier"), str) and turn.get("tier"):
            reply_extra["tier"] = turn["tier"]
        if reply_extra:
            reply["extra"] = reply_extra
        steps.append(reply)
    if not steps:
        raise AtifExportError("session has no steps to export")
    root_extra: dict[str, object] = {"sonder_source": "memory_interactions"}
    if extra:
        root_extra.update(extra)
    document = {
        "schema_version": ATIF_SCHEMA_VERSION,
        "session_id": session_id,
        "trajectory_id": session_id,
        "agent": (agent or AtifAgent()).to_dict(),
        "steps": steps,
        "final_metrics": _final_metrics(steps),
        "extra": root_extra,
    }
    return redact_atif(document)


# --------------------------------------------------------------------------
# Structural validation (mirrors harbor.models.trajectories, ATIF-v1.7)
# --------------------------------------------------------------------------

_ALLOWED = {
    "trajectory": {"schema_version", "session_id", "trajectory_id", "agent", "steps", "notes",
                   "final_metrics", "continued_trajectory_ref", "extra", "subagent_trajectories"},
    "agent": {"name", "version", "model_name", "tool_definitions", "extra"},
    "step": {"step_id", "timestamp", "source", "model_name", "reasoning_effort", "message",
             "reasoning_content", "tool_calls", "observation", "metrics", "is_copied_context",
             "llm_call_count", "extra"},
    "tool_call": {"tool_call_id", "function_name", "arguments", "extra"},
    "observation": {"results"},
    "result": {"source_call_id", "content", "subagent_trajectory_ref", "extra"},
    "ref": {"trajectory_id", "session_id", "trajectory_path", "extra"},
    "metrics": {"prompt_tokens", "completion_tokens", "cached_tokens", "cost_usd",
                "prompt_token_ids", "completion_token_ids", "logprobs", "extra"},
    "final_metrics": {"total_prompt_tokens", "total_completion_tokens", "total_cached_tokens",
                      "total_cost_usd", "total_steps", "extra"},
}
_SCHEMA_VERSIONS = _VERBATIM_VALUES["schema_version"]
_AGENT_ONLY = ("model_name", "reasoning_effort", "reasoning_content", "tool_calls", "metrics")


def _unknown(value: Mapping[str, object], kind: str, where: str, errors: list[str]) -> None:
    for key in sorted(set(value) - _ALLOWED[kind]):
        errors.append("%s: undeclared field %r" % (where, key))


def _is_int(value: object) -> bool:
    return type(value) is int


def _optional_int_fields(value: Mapping[str, object], fields: Sequence[str], where: str,
                         errors: list[str]) -> None:
    for name in fields:
        if name in value and value[name] is not None and not _is_int(value[name]):
            errors.append("%s.%s must be an integer" % (where, name))


def validate_atif(document: object, where: str = "trajectory") -> list[str]:
    """Return every ATIF-v1.7 violation in ``document`` (empty when valid).

    Mirrors the Harbor Pydantic models: required fields and types, undeclared
    fields rejected, sequential ``step_id`` from 1, agent-only step fields,
    the ``llm_call_count == 0`` rule, ``source_call_id`` linkage within a
    step, unique non-null embedded ``trajectory_id``s, resolvable subagent
    references, and ISO 8601 timestamps.
    """
    errors: list[str] = []
    if not isinstance(document, Mapping):
        return ["%s must be an object" % where]
    _unknown(document, "trajectory", where, errors)
    if document.get("schema_version") not in _SCHEMA_VERSIONS:
        errors.append("%s.schema_version is not a known ATIF version" % where)
    for name in ("session_id", "trajectory_id", "notes", "continued_trajectory_ref"):
        if name in document and document[name] is not None and not isinstance(document[name], str):
            errors.append("%s.%s must be a string" % (where, name))
    agent = document.get("agent")
    if not isinstance(agent, Mapping):
        errors.append("%s.agent is required" % where)
    else:
        _unknown(agent, "agent", where + ".agent", errors)
        for name in ("name", "version"):
            if not isinstance(agent.get(name), str):
                errors.append("%s.agent.%s must be a string" % (where, name))
        if agent.get("model_name") is not None and not isinstance(agent.get("model_name"), str):
            errors.append("%s.agent.model_name must be a string" % where)
    steps = document.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("%s.steps must be a non-empty array" % where)
        steps = []
    embedded_ids: set[str] = set()
    subagents = document.get("subagent_trajectories")
    if subagents is not None:
        if not isinstance(subagents, list):
            errors.append("%s.subagent_trajectories must be an array" % where)
        else:
            for index, sub in enumerate(subagents):
                sub_where = "%s.subagent_trajectories[%d]" % (where, index)
                errors.extend(validate_atif(sub, sub_where))
                trajectory_id = sub.get("trajectory_id") if isinstance(sub, Mapping) else None
                if not isinstance(trajectory_id, str):
                    errors.append("%s.trajectory_id is required for embedded subagents" % sub_where)
                elif trajectory_id in embedded_ids:
                    errors.append("%s.trajectory_id %r is not unique" % (sub_where, trajectory_id))
                else:
                    embedded_ids.add(trajectory_id)
    for index, step in enumerate(steps):
        step_where = "%s.steps[%d]" % (where, index)
        if not isinstance(step, Mapping):
            errors.append("%s must be an object" % step_where)
            continue
        _unknown(step, "step", step_where, errors)
        if not _is_int(step.get("step_id")) or step.get("step_id") != index + 1:
            errors.append("%s.step_id: expected %d (sequential from 1)" % (step_where, index + 1))
        source = step.get("source")
        if source not in ("system", "user", "agent"):
            errors.append("%s.source must be system, user or agent" % step_where)
        message = step.get("message")
        if not isinstance(message, (str, list)):
            errors.append("%s.message is required (string or content parts)" % step_where)
        if "timestamp" in step and step["timestamp"] is not None and _iso(step["timestamp"]) is None:
            errors.append("%s.timestamp is not ISO 8601" % step_where)
        if source != "agent":
            for name in _AGENT_ONLY:
                if step.get(name) is not None:
                    errors.append("%s.%s is only applicable when source is agent" % (step_where, name))
        count = step.get("llm_call_count")
        if count is not None and (not _is_int(count) or count < 0):
            errors.append("%s.llm_call_count must be a non-negative integer" % step_where)
        if count == 0 and source == "agent":
            for name in ("metrics", "reasoning_content"):
                if step.get(name) is not None:
                    errors.append("%s.%s must be absent when llm_call_count is 0" % (step_where, name))
        call_ids: set[str] = set()
        tool_calls = step.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                errors.append("%s.tool_calls must be an array" % step_where)
                tool_calls = []
            for call_index, call in enumerate(tool_calls):
                call_where = "%s.tool_calls[%d]" % (step_where, call_index)
                if not isinstance(call, Mapping):
                    errors.append("%s must be an object" % call_where)
                    continue
                _unknown(call, "tool_call", call_where, errors)
                if not isinstance(call.get("tool_call_id"), str):
                    errors.append("%s.tool_call_id must be a string" % call_where)
                else:
                    call_ids.add(call["tool_call_id"])
                if not isinstance(call.get("function_name"), str):
                    errors.append("%s.function_name must be a string" % call_where)
                if not isinstance(call.get("arguments"), Mapping):
                    errors.append("%s.arguments must be an object" % call_where)
        observation = step.get("observation")
        if observation is not None:
            if not isinstance(observation, Mapping) or not isinstance(observation.get("results"), list):
                errors.append("%s.observation.results must be an array" % step_where)
            else:
                _unknown(observation, "observation", step_where + ".observation", errors)
                for result_index, result in enumerate(observation["results"]):
                    result_where = "%s.observation.results[%d]" % (step_where, result_index)
                    if not isinstance(result, Mapping):
                        errors.append("%s must be an object" % result_where)
                        continue
                    _unknown(result, "result", result_where, errors)
                    source_call = result.get("source_call_id")
                    if source_call is not None and source_call not in call_ids:
                        errors.append("%s.source_call_id %r is not in step %s's tool_calls"
                                      % (result_where, source_call, step.get("step_id")))
                    content = result.get("content")
                    if content is not None and not isinstance(content, (str, list)):
                        errors.append("%s.content must be a string or content parts" % result_where)
                    for ref_index, ref in enumerate(result.get("subagent_trajectory_ref") or ()):
                        ref_where = "%s.subagent_trajectory_ref[%d]" % (result_where, ref_index)
                        if not isinstance(ref, Mapping):
                            errors.append("%s must be an object" % ref_where)
                            continue
                        _unknown(ref, "ref", ref_where, errors)
                        if ref.get("trajectory_id") is None and ref.get("trajectory_path") is None:
                            errors.append("%s must set trajectory_id or trajectory_path" % ref_where)
                        elif (ref.get("trajectory_path") is None
                              and ref.get("trajectory_id") not in embedded_ids):
                            errors.append("%s.trajectory_id %r matches no embedded subagent"
                                          % (ref_where, ref.get("trajectory_id")))
        metrics = step.get("metrics")
        if metrics is not None:
            if not isinstance(metrics, Mapping):
                errors.append("%s.metrics must be an object" % step_where)
            else:
                _unknown(metrics, "metrics", step_where + ".metrics", errors)
                _optional_int_fields(metrics, ("prompt_tokens", "completion_tokens", "cached_tokens"),
                                     step_where + ".metrics", errors)
    final = document.get("final_metrics")
    if final is not None:
        if not isinstance(final, Mapping):
            errors.append("%s.final_metrics must be an object" % where)
        else:
            _unknown(final, "final_metrics", where + ".final_metrics", errors)
            _optional_int_fields(final, ("total_prompt_tokens", "total_completion_tokens",
                                         "total_cached_tokens", "total_steps"),
                                 where + ".final_metrics", errors)
    return errors


__all__ = [
    "ATIF_FORMAT", "ATIF_SCHEMA_VERSION", "AtifAgent", "AtifExportError", "MAX_EMBEDDED_SUBAGENTS",
    "interaction_turns_to_atif", "redact_atif", "session_events_to_atif", "validate_atif",
]
