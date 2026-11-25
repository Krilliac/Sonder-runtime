"""HTTP chat work-intent, model-catalog, structured-schema, and completion formatters.

Extracted from ``sonder_runtime/interfaces/http/serve.py``.  Imports only
``sonder_runtime.application``, ``sonder_runtime.interfaces``, and non-IO
standard-library modules.  Generation call sites (``_run_prompt``,
``_run_structured_prompt``) remain in ``serve.py`` to preserve call-site
registry and AST admission invariants.
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import json
import math
import os
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

from sonder_runtime.application.chat.handoff_receipts import (
    ChatWorkReceiptService,
    ChatWorkResult,
)
from sonder_runtime.application.chat.lanes import (
    ChatHandoffProvenance,
    ChatLaneService,
)
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.provider_attempts import (
    ProviderCaptureFailure,
    deferred_provider_request_scope,
    telemetry_error_code,
)
from ..work_runs import WorkCapacityExhausted, current_run_id as current_work_run_id
from .config_wiring import HTTPRequestError, LiveSessionCaptureFailure, TurnResult
from .model_catalog import annotate_model_rows

STRUCTURED_SCHEMA_KEYS = frozenset({
    "type", "enum", "const", "required", "properties",
    "additionalProperties", "minProperties", "maxProperties", "items",
    "minItems", "maxItems", "uniqueItems", "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
})
STRUCTURED_TYPES = frozenset({
    "object", "array", "string", "integer", "number", "boolean", "null",
})
STRUCTURED_SCHEMA_MAX_DEPTH = 16
STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS = 256


def work_run_stop_reason(lifecycle_get: Callable[[], Any]) -> str:
    """Stop every work run's effects once the runtime starts draining."""
    try:
        draining = lifecycle_get().coordinator.draining
    except Exception:
        return "the runtime lifecycle could not be read"
    return "the runtime is draining for shutdown" if draining else ""


@contextlib.contextmanager
def work_run_lifetime(
    lifecycle_get: Callable[[], Any], logger: Any,
) -> Iterator[None]:
    """Count a work run as an in-flight mutation for the graceful drain."""
    coordinator: Any = None
    counted = False
    try:
        coordinator = lifecycle_get().coordinator
        counted = bool(coordinator.begin_mutation())
    except Exception:
        logger.warning(
            "work run could not be counted for graceful drain", exc_info=True,
        )
    try:
        yield
    finally:
        if counted and coordinator is not None:
            coordinator.end_mutation()


def work_run_record(
    result: Any, *, work_run_outcomes: frozenset[str] | set[str] | tuple[str, ...],
) -> tuple[str, str]:
    """Durable ``(status, answer)`` for one finished work run."""
    if isinstance(result, ChatWorkResult):
        status = result.status if result.status in work_run_outcomes else "unknown"
        return status, result.text
    if isinstance(result, str):
        return "refused", result
    return "unknown", ""


def work_run_pending_text(run_id: str, *, budget_seconds: int) -> str:
    """Client-neutral pending message while a routed work run continues."""
    return (
        "Work is still running as work run %s (wall-clock budget %d s); "
        "check on it or cancel it from your client." % (run_id, budget_seconds)
    )


def bind_current_activity(
    activity_tracker: Any, chat_effort: Any,
) -> Callable[[Callable[[], Any]], Callable[[], None]] | None:
    """Carry the request's activity span and reasoning effort into a worker thread."""
    response_id = activity_tracker.current_response_id()
    effort = chat_effort.active_turn_effort()
    if not response_id and effort is None:
        return None

    def wrap(body: Callable[[], Any]) -> Callable[[], None]:
        def bound() -> None:
            with contextlib.ExitStack() as stack:
                if response_id:
                    stack.enter_context(activity_tracker.bind_response(response_id))
                if effort is not None:
                    stack.enter_context(chat_effort.effort_scope(effort))
                body()

        return bound

    return wrap


def append_session_transcript_event(
    session_id: str,
    event_type: str,
    content: Any,
    *,
    turn_id: str,
    request_id: str = "",
    extra: Any = None,
    default_app_fn: Callable[[], Any],
    is_valid_session_id_fn: Callable[[str], bool],
    redact_text: Callable[[str], str],
    logger: Any,
) -> None:
    """Best-effort redacted append to the durable session event log for UI/TUI sync."""
    if not session_id or not isinstance(content, str) or not content.strip():
        return
    try:
        if not is_valid_session_id_fn(session_id):
            return
        payload: dict[str, Any] = {
            "content": redact_text(content),
            "turn_id": turn_id,
        }
        if request_id:
            payload["request_id"] = request_id
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key not in payload and value is not None:
                    payload[key] = value
        default_app_fn().session_repository().append(session_id, event_type, payload)
    except Exception:
        logger.debug("session transcript append skipped (%s)", event_type, exc_info=True)


def handle_work_intent(
    serve_mod: Any,
    content: str,
    *,
    project: str = "",
    authorized: bool = False,
    context: Any = None,
    idempotency_key: str = "",
    session_id: str = "",
    session_ref: str = "",
    correlation_id: str = "",
    with_receipt: bool = False,
    tool_tier: str = "",
    record_transcript: bool = False,
    default_app_fn: Callable[[], Any],
    work_ack_fn: Callable[..., Any],
    run_work_scoped_fn: Callable[..., Any],
    classify_work_output_fn: Callable[..., tuple[str, str]],
    start_narrated_work_fn: Callable[..., Any],
    work_admission_result_fn: Callable[..., Any],
    http_work_runs_mod: Any,
    activity_tracker_mod: Any,
    admission_rejected_cls: Any,
) -> Any:
    """Route developer work through the bounded execution-mode chooser."""
    refusal = serve_mod.intents.containment_egress_refusal(content)
    if refusal:
        return ChatWorkResult(refusal, "refused") if with_receipt else refusal
    if not authorized:
        return None
    worker_cap = serve_mod.server.master_orchestrator.requested_worker_cap(content)
    classified_intent = (
        None if worker_cap else serve_mod.intents.classify_execution(content)
    )
    if tool_tier and (
        not classified_intent
        or classified_intent["mode"] not in {"inspection", "workbench"}
    ):
        return None
    if not worker_cap and not classified_intent:
        return None
    action = "natural-work\0%s\0%s" % (project, content)
    task_scope = None if context is None else serve_mod._task_account_scope(context)

    def _route_work(**extra: Any) -> Any:
        scope_ctx = (
            serve_mod.server.scoped_task_account_boundary(task_scope)
            if task_scope is not None
            else contextlib.nullcontext()
        )
        with scope_ctx:
            return serve_mod.server.route_work_request(
                content,
                project=project,
                _classified_intent=classified_intent,
                **extra,
            )

    if with_receipt:
        action += f"\0session={session_id}"
        acknowledgement = work_ack_fn(
            serve_mod.server, content, worker_cap, classified_intent, project,
        )

        def run_admitted_work() -> ChatWorkResult:
            try:
                receipts = ChatWorkReceiptService(default_app_fn().session_repository())
                source = receipts.source(session_id)
            except Exception as error:
                raise LiveSessionCaptureFailure from error
            decision = ChatLaneService(serve_mod.intents.classify_execution).decide(
                content,
                project=project,
                durable_context_refs=(source.handoff_ref,) if source else (),
                provenance=ChatHandoffProvenance(
                    surface="served-http",
                    reason="natural work admission",
                    correlation_id=correlation_id,
                ),
                intent_override=(
                    {
                        "mode": "fleet",
                        "reason": "explicit bounded worker-count request",
                        "plan_only": False,
                    }
                    if worker_cap
                    else classified_intent
                ),
            )
            if not decision.is_execution or decision.handoff is None:
                raise ValueError("admitted work must have an execution handoff")
            try:
                admission = receipts.admit(
                    session_id,
                    decision.handoff,
                    source,
                    routing_reason=decision.reason,
                )
            except Exception as error:
                raise LiveSessionCaptureFailure from error
            work_turn_id = uuid.uuid4().hex if record_transcript else ""
            if record_transcript:
                serve_mod._append_session_transcript_event(
                    session_id,
                    "user.message",
                    content,
                    turn_id=work_turn_id,
                    request_id=correlation_id,
                    extra={"lane": decision.lane, "work_run_id": current_work_run_id()},
                )
            try:
                output = run_work_scoped_fn(
                    current_work_run_id(),
                    http_work_runs_mod.link_run,
                    acknowledgement,
                    activity_tracker_mod.current_response_id() or "",
                    lambda: _route_work(
                        _admitted_decision=decision,
                        **({"_tool_tier": tool_tier} if tool_tier else {}),
                    ),
                )
            except BaseException:
                with contextlib.suppress(Exception):
                    receipts.finish(session_id, admission, "unknown")
                raise
            status, child_run_id = classify_work_output_fn(
                output, http_work_runs_mod, current_work_run_id(),
            )
            try:
                terminal = receipts.finish(session_id, admission, status)
            except Exception as error:
                raise LiveSessionCaptureFailure from error
            if record_transcript and isinstance(output, str) and output.strip():
                serve_mod._append_session_transcript_event(
                    session_id,
                    "model.response",
                    output,
                    turn_id=work_turn_id,
                    request_id=correlation_id,
                    extra={
                        "lane": decision.lane,
                        "work_run_id": current_work_run_id(),
                        "status": status,
                    },
                )
            return ChatWorkResult(
                output if isinstance(output, str) else "",
                status,
                decision.lane,
                session_ref,
                admission.event_id,
                terminal.event_id,
                source.event_id if source else "",
                routing_reason=decision.reason,
                work_run_id=current_work_run_id(),
                child_run_id=child_run_id,
            )

        try:
            return work_admission_result_fn(
                serve_mod._idempotent_http_action(
                    context,
                    idempotency_key,
                    action,
                    lambda: start_narrated_work_fn(
                        serve_mod._WORK_RUNNER,
                        serve_mod._state_principal(context),
                        run_admitted_work,
                        acknowledgement,
                        http_work_runs_mod,
                        classify=serve_mod._work_run_record,
                        thread_wrapper=serve_mod._bind_current_activity(),
                        session_ref=session_ref,
                    ),
                ),
                session_ref,
            )
        except WorkCapacityExhausted as error:
            raise admission_rejected_cls(
                429,
                "WORK_CAPACITY_EXHAUSTED",
                "routed work capacity is busy (%s); retry later, or cancel a run "
                "with POST /v1/work-runs/<id>/cancel" % error,
                retryable=True,
            ) from None
    return serve_mod._idempotent_http_action(
        context,
        idempotency_key,
        action,
        lambda: _route_work(
            **({"_tool_tier": tool_tier} if tool_tier else {}),
        ),
    )


def model_to_tier(model: Any, *, runtime: Any) -> str | None:
    """Map an OpenAI ``model`` field to a known Sonder Runtime tier, else ``None``."""
    cleaned = (model or "").strip()
    if not cleaned or cleaned == "sonder" or cleaned.startswith("gpt-"):
        return None
    if cleaned in runtime.TIERS:
        return str(cleaned)
    return None


def uses_default_model_route(model: Any) -> bool:
    """Syntactic check: True when *model* leaves selection to Sonder's default router."""
    cleaned = str(model or "").strip()
    return not cleaned or cleaned in ("sonder", "local") or cleaned.startswith("gpt-")


def request_model_selector(serve_mod: Any, model: Any) -> str | None:
    """Resolve explicit tier or discovered model selector while keeping OpenAI defaults."""
    selected = serve_mod._model_to_tier(model)
    if selected is not None:
        return str(selected)
    raw = str(model or "").strip()
    if raw:
        try:
            discovered = serve_mod.server.resolve_discovered_model(raw)
        except Exception:
            discovered = None
        if discovered:
            return str(discovered)
    if not raw or raw in ("sonder", "local") or raw.startswith("gpt-"):
        return None
    return raw


def chat_model_selection_error(
    serve_mod: Any, selector: Any,
) -> tuple[int, str] | None:
    """Return ``(status, message)`` for an explicit selector that cannot chat, else ``None``."""
    if not selector:
        return None
    image_gen = serve_mod._image_generation
    if image_gen.is_image_generation_model_name(selector):
        env_img = os.environ.get("SONDER_IMAGE_MODEL", "").strip()
        if env_img and env_img.casefold() == str(selector).strip().casefold():
            return None
        discovered_imgs = image_gen.list_discovered_image_models()
        if any(
            name.casefold() == str(selector).strip().casefold()
            for name, _ in discovered_imgs
        ):
            return None
    try:
        target, cloud, _augment, tier_label = serve_mod.server._serve_target(selector, None)
        if tier_label == "cloud-disabled":
            return 403, "cloud model access is disabled by the operator"
        if tier_label is not None:
            unfit = (
                ""
                if cloud or not target
                else serve_mod.server.chat_model_unfit_reason(target)
            )
            if unfit:
                return 400, "model is not chat-capable (%s); choose another model" % unfit
            return None
        found = serve_mod.server.resolve_discovered_model_record(selector)
    except Exception:
        return 503, "live model catalog is temporarily unavailable; retry shortly"
    if found:
        _name, record = found
        reason = serve_mod.server._fanout_nonchat_reason(record)
        if reason:
            return 400, "model is not chat-capable (%s)" % reason
    return 400, "unknown model '%s'" % selector


def openai_model_rows(serve_mod: Any, *, gateway: Any) -> list[dict[str, Any]]:
    """Return stable, safe OpenAI-shaped model metadata for chat clients."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(identifier: Any, owned_by: str) -> None:
        cleaned = str(identifier or "").strip()
        key = cleaned.casefold()
        if not cleaned or key in seen:
            return
        seen.add(key)
        rows.append({"id": cleaned, "object": "model", "owned_by": owned_by})

    runtime = serve_mod.server
    image_gen = serve_mod._image_generation
    try:
        records = runtime.servable_model_records()
    except Exception:
        records = ()

    add("sonder", "local")
    for tier_name, model in runtime.available_tiers().items():
        cloud = runtime._is_cloud_tier(tier_name, model)
        if not cloud and (
            runtime._runtime_model_capability_error(tier_name, model, records)
            or runtime.chat_model_unfit_reason(model)
        ):
            continue
        add(tier_name, "cloud" if cloud else "local")
    for name, record in records:
        if runtime._fanout_nonchat_reason(record):
            if image_gen.is_image_generation_model_name(name):
                add(name, "local")
            continue
        cloud = runtime._is_cloud_model_name(name)
        if (cloud and not runtime.cloud_allowed()) or (
            not cloud and runtime.chat_model_unfit_reason(name)
        ):
            continue
        add(name, "cloud" if cloud else "local")
    for img_name, _origin in image_gen.list_discovered_image_models():
        add(img_name, "local")
    env_img_model = os.environ.get("SONDER_IMAGE_MODEL", "").strip()
    if env_img_model:
        add(env_img_model, "local")
    return annotate_model_rows(
        rows,
        {"sonder", *runtime.available_tiers()},
        serve_target=runtime._serve_target,
        bridge_provider=runtime._bridge_provider_for_tier,
        gateway=gateway,
    )


def reasoning_audience() -> str:
    """Who may receive model reasoning over HTTP: ``'developer'`` or ``'all'``."""
    value = os.environ.get("SONDER_REASONING_AUDIENCE", "").strip().lower()
    return "all" if value == "all" else "developer"


def reasoning_visible_to(serve_mod: Any, context: Any) -> bool:
    """Whether model reasoning may be included in HTTP responses for *context*."""
    if not serve_mod.server.reasoning_exposure_enabled():
        return False
    if reasoning_audience() == "all":
        return True
    return bool(serve_mod._developer_authorized(context))


def turn_reasoning(runtime: Any) -> str:
    """Reasoning recorded by the active activity span, when exposure is enabled."""
    if not runtime.reasoning_exposure_enabled():
        return ""
    record = runtime.activity_tracker.current_reasoning()
    if not record:
        return ""
    return str(record.get("text") or "")


def capture_http_provider_request(
    function: Callable[..., Any],
    *,
    serve_mod: Any,
    default_app_fn: Callable[[], Any],
) -> Callable[..., Any]:
    """Wrap ``_run_prompt`` / ``_run_structured_prompt`` with deferred provider capture."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        enabled = bool(values.get("session") and values.get("capture_request_id"))

        def admit() -> tuple[Any, Any]:
            requested_tier = values.get("tier")
            captured_tier = requested_tier
            if not captured_tier:
                state = values.get("state")
                strict = state.strict if state is not None else None
                _, _, _, captured_tier = serve_mod.server._serve_target(None, strict)
            capture = default_app_fn().session_capture_service()
            pending = capture.begin_request(
                values["session"],
                values["capture_turn_id"],
                ModelRequest(
                    prompt=values["prompt"],
                    tier=captured_tier or "sonder",
                    history=tuple(values.get("history") or ()),
                    options={"stream": bool(values.get("capture_stream"))},
                    routing_metadata=serve_mod._chat_routing_metadata(values.get("tier")),
                ),
                request_id=values["capture_request_id"],
                user_message=values["prompt"],
            )
            return capture, pending

        try:
            with deferred_provider_request_scope(admit if enabled else None) as scope:
                try:
                    result = function(*args, **kwargs)
                except ProviderCaptureFailure:
                    raise
                except Exception as error:
                    if enabled:
                        serve_mod._fail_live_session_request(
                            getattr(scope, "admission", None),
                            values["session"],
                            values["capture_request_id"],
                            telemetry_error_code(error),
                        )
                    raise
                if enabled and isinstance(result, TurnResult):
                    result = replace(result, provider_capture=scope.admission)
                return result
        except ProviderCaptureFailure as error:
            raise LiveSessionCaptureFailure from error

    return wrapped


def _response_format_error(message: str) -> HTTPRequestError:
    return HTTPRequestError(400, message)


def validate_structured_schema(
    schema: Any,
    depth: int = 0,
    *,
    unique_items_max_fn: Callable[[], int],
) -> None:
    """Reject schema features that the HTTP structured endpoint cannot prove post-hoc."""
    if depth > STRUCTURED_SCHEMA_MAX_DEPTH:
        raise _response_format_error("response_format schema is nested too deeply")
    if not isinstance(schema, dict):
        raise _response_format_error("response_format schema nodes must be objects")
    unknown = set(schema) - STRUCTURED_SCHEMA_KEYS
    if unknown:
        raise _response_format_error(
            "response_format schema uses unsupported keywords: %s"
            % ", ".join(sorted(unknown)),
        )
    declared = schema.get("type")
    if not isinstance(declared, str) or declared not in STRUCTURED_TYPES:
        raise _response_format_error(
            "every response_format schema node needs one supported type",
        )
    if "required" in schema and (
        not isinstance(schema["required"], list)
        or any(not isinstance(key, str) for key in schema["required"])
    ):
        raise _response_format_error("schema required must be an array of strings")
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict) or any(
            not isinstance(key, str) for key in properties
        ):
            raise _response_format_error("schema properties must be an object")
        for child in properties.values():
            validate_structured_schema(
                child, depth + 1, unique_items_max_fn=unique_items_max_fn,
            )
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if isinstance(extra, dict):
            validate_structured_schema(
                extra, depth + 1, unique_items_max_fn=unique_items_max_fn,
            )
        elif not isinstance(extra, bool):
            raise _response_format_error(
                "additionalProperties must be boolean or a schema",
            )
    if "items" in schema:
        validate_structured_schema(
            schema["items"], depth + 1, unique_items_max_fn=unique_items_max_fn,
        )
    for keyword in (
        "minProperties", "maxProperties", "minItems", "maxItems", "minLength", "maxLength",
    ):
        if keyword in schema and (
            isinstance(schema[keyword], bool)
            or not isinstance(schema[keyword], int)
            or schema[keyword] < 0
        ):
            raise _response_format_error("%s must be a non-negative integer" % keyword)
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise _response_format_error("uniqueItems must be a boolean")
    if schema.get("uniqueItems") is True:
        maximum = schema.get("maxItems")
        limit = unique_items_max_fn()
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            raise _response_format_error(
                "uniqueItems=true requires an integer maxItems no greater than %d" % limit,
            )
        if maximum > limit:
            raise _response_format_error(
                "uniqueItems=true requires maxItems no greater than %d" % limit,
            )
    for keyword in (
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    ):
        if keyword in schema and (
            isinstance(schema[keyword], bool)
            or not isinstance(schema[keyword], (int, float))
            or (isinstance(schema[keyword], float) and not math.isfinite(schema[keyword]))
        ):
            raise _response_format_error("%s must be a number" % keyword)
    if "multipleOf" in schema and schema["multipleOf"] <= 0:
        raise _response_format_error("multipleOf must be positive")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise _response_format_error("enum must be an array")


def response_format_schema(
    value: Any, *, unique_items_max_fn: Callable[[], int],
) -> dict[str, Any]:
    """Validate and return the host-verifiable JSON schema for ``response_format``."""
    if not isinstance(value, dict):
        raise _response_format_error("response_format must be an object")
    kind = value.get("type")
    if kind == "json_object" and set(value) == {"type"}:
        return {"type": "object"}
    if kind != "json_schema" or set(value) != {"type", "json_schema"}:
        raise _response_format_error(
            "response_format must be {'type':'json_object'} or a strict json_schema object",
        )
    envelope = value["json_schema"]
    if not isinstance(envelope, dict) or set(envelope) != {"name", "schema", "strict"}:
        raise _response_format_error(
            "json_schema must contain exactly name, schema, and strict",
        )
    if (
        not isinstance(envelope["name"], str)
        or not envelope["name"].strip()
        or len(envelope["name"]) > 64
        or envelope["strict"] is not True
    ):
        raise _response_format_error(
            "json_schema requires a non-empty name (at most 64 characters) and strict=true",
        )
    schema = envelope["schema"]
    validate_structured_schema(schema, unique_items_max_fn=unique_items_max_fn)
    if not isinstance(schema, dict):
        raise _response_format_error("response_format schema nodes must be objects")
    return schema


def receipt_text(value: Any) -> str:
    """Bound and sanitize a provider/configuration label for response metadata."""
    return "".join(
        char if char >= " " and char != "\x7f" else "?"
        for char in str(value or "")[:256]
    )


def chat_completion_object(
    content: Any,
    *,
    model: str = "sonder",
    iid: str | None = None,
    reasoning: str = "",
    elapsed_ms: int | float | None = None,
    receipt: Any = None,
    activity_response: Any = None,
    runtime: Any,
    chat_usage_fn: Callable[[Any], Any],
) -> dict[str, Any]:
    """Assemble an OpenAI-compatible ``chat.completion`` JSON payload."""
    completion_id = iid or uuid.uuid4().hex[:12]
    activity = (
        runtime.activity_tracker.public_response(
            activity_response, include_detail=False,
        )
        if isinstance(activity_response, dict)
        else None
    )
    obj: dict[str, Any] = {
        "id": "chatcmpl-%s" % completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": (
                    "length"
                    if isinstance(receipt, dict) and receipt.get("truncated")
                    else "stop"
                ),
            }
        ],
        "usage": chat_usage_fn(activity_response),
        "sonder_activity": activity,
    }
    if reasoning:
        obj["sonder_reasoning"] = reasoning
    if elapsed_ms is not None:
        obj["sonder_elapsed_ms"] = max(0, int(elapsed_ms))
    if receipt:
        obj["sonder_receipt"] = receipt
    return obj


def format_sse_chunk(
    iid: str,
    model: str,
    delta: Any,
    finish_reason: str | None = None,
    elapsed_ms: int | float | None = None,
    receipt: Any = None,
    usage: Any = None,
    activity: Any = None,
    sonder_tool: Any = None,
    sonder_agent: Any = None,
    sonder_agent_board: Any = None,
) -> str:
    """Serialize one ``chat.completion.chunk`` Server-Sent Event frame."""
    obj: dict[str, Any] = {
        "id": "chatcmpl-%s" % iid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": (
            []
            if usage is not None and not delta and finish_reason is None
            else [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
        ),
    }
    if elapsed_ms is not None:
        obj["sonder_elapsed_ms"] = max(0, int(elapsed_ms))
    if receipt:
        obj["sonder_receipt"] = receipt
    if usage is not None:
        obj["usage"] = usage
    if activity is not None:
        obj["sonder_activity"] = activity
    if sonder_tool is not None:
        obj["sonder_tool"] = sonder_tool
    if sonder_agent is not None:
        obj["sonder_agent"] = sonder_agent
    if sonder_agent_board is not None:
        obj["sonder_agent_board"] = sonder_agent_board
    return "data: %s\n\n" % json.dumps(obj)


def run_http_work_slash(
    arg: str,
    *,
    project: str,
    workspace_project: Any,
    context: Any,
    idempotency_key: str,
    server: Any,
    account_boundary_refusal: Callable[..., str],
    idempotent_action: Callable[..., Any],
    narrate_command: Callable[..., Any],
    fanout_owner: Callable[[Any], str],
    fanout_role: Callable[[Any], str],
    developer_authorized: Callable[[Any], bool],
    invoke: Callable[[str, int, str], Any],
    task_scope_of: Callable[[Any], str | None] | None = None,
) -> Any:
    """Handle HTTP ``/work`` and ``/agent`` with optional secondary multi-model/fleet modes."""
    from sonder_runtime.application.agents.delegation import rejected_project_refusal

    parsed_mode = server._parse_agent_mode_arg(arg)
    if parsed_mode.usage_error:
        return parsed_mode.usage_error
    task_boundary_error = account_boundary_refusal("workbench_agent", {}, context)
    if task_boundary_error:
        return task_boundary_error
    task_scope = (
        None if (context is None or task_scope_of is None) else task_scope_of(context)
    )
    work_project = project if workspace_project is None else workspace_project
    rejected = rejected_project_refusal(work_project, server.served_work_project)
    if rejected:
        return rejected

    def _run() -> Any:
        scope_ctx = (
            server.scoped_task_account_boundary(task_scope)
            if task_scope is not None
            else contextlib.nullcontext()
        )
        with scope_ctx:
            prepared, prep_err = server._prepare_agent_mode_run(
                arg,
                request_owner="" if context is None else fanout_owner(context),
                request_role="" if context is None else fanout_role(context),
                allow_fanout=False if context is None else developer_authorized(context),
            )
            if prep_err:
                return prep_err
            return invoke(prepared.prompt, prepared.max_steps, work_project)

    return idempotent_action(
        context,
        idempotency_key,
        f"workbench\0{work_project}\0{arg.strip()}",
        lambda: narrate_command(
            "workbench_agent",
            {"prompt": parsed_mode.task, "mode": parsed_mode.mode or "single", "project": work_project},
            _run,
            context,
        ),
    )


__all__ = [
    "STRUCTURED_SCHEMA_KEYS",
    "STRUCTURED_SCHEMA_MAX_DEPTH",
    "STRUCTURED_TYPES",
    "STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS",
    "append_session_transcript_event",
    "bind_current_activity",
    "capture_http_provider_request",
    "chat_completion_object",
    "chat_model_selection_error",
    "format_sse_chunk",
    "handle_work_intent",
    "model_to_tier",
    "openai_model_rows",
    "reasoning_audience",
    "reasoning_visible_to",
    "receipt_text",
    "request_model_selector",
    "response_format_schema",
    "run_http_work_slash",
    "turn_reasoning",
    "uses_default_model_route",
    "validate_structured_schema",
    "work_run_lifetime",
    "work_run_pending_text",
    "work_run_record",
    "work_run_stop_reason",
]
