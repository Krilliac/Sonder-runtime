"""Thin HTTP handlers (SPEC-5 §28).

Each handler: parse request → create OperationContext → call service → map errors → JSON response.
No business logic, no direct adapter access.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from ...application.context import OperationContext, local_owner_context
from ...application.errors import (
    Cancelled,
    DeadlineExceeded,
    Forbidden,
    InvalidInput,
    NotFound,
    SonderError,
)

import logging
logger = logging.getLogger(__name__)


class RequestLike(Protocol):
    @property
    def method(self) -> str: ...
    @property
    def path(self) -> str: ...
    @property
    def body(self) -> bytes: ...
    @property
    def headers(self) -> dict[str, str]: ...


class Response:
    __slots__ = ("status", "body", "headers")

    def __init__(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {"Content-Type": "application/json"}

    def serialize(self) -> bytes:
        return json.dumps(self.body).encode("utf-8")


ERROR_STATUS = {
    "INVALID_INPUT": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "CONCURRENCY_CONFLICT": 409,
    "CAPACITY_EXCEEDED": 429,
    "DEADLINE_EXCEEDED": 504,
    "CANCELLED": 499,
    "DEPENDENCY_UNAVAILABLE": 503,
    "INTERNAL_FAILURE": 500,
    "INTEGRITY_FAILURE": 500,
    "MIGRATION_REQUIRED": 503,
}


def error_response(err: SonderError) -> Response:
    status = ERROR_STATUS.get(err.code, 500)
    if status >= 500:
        logger.error(f"server error response: code={err.code!r}, status={status}, retryable={err.retryable}")
    logger.debug(f"error_response: code={err.code!r}, status={status}")
    return Response(status, {"error": err.code, "message": str(err), "retryable": err.retryable})


def context_from_request(
    request: RequestLike,
    *,
    timeout_seconds: float = 30.0,
) -> OperationContext:
    correlation = request.headers.get(
        "X-Correlation-Id", uuid.uuid4().hex,
    )
    logger.debug(f"context_from_request: method={request.method!r}, path={request.path!r}, correlation={correlation!r}")
    return local_owner_context(
        correlation_id=correlation,
        source="http",
        timeout_seconds=timeout_seconds,
    )


class HealthHandler:
    """GET /health — no application service needed."""

    def handle(self, request: RequestLike) -> Response:
        logger.debug("HealthHandler.handle: responding 200 ok")
        return Response(200, {"status": "ok"})


class RecallHandler:
    """POST /v1/recall — delegates to RecallService."""

    def __init__(self, recall_service):
        self._recall = recall_service

    def handle(self, request: RequestLike) -> Response:
        logger.debug("RecallHandler.handle: incoming recall request")
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("RecallHandler.handle: bad JSON in request body")
            return Response(400, {"error": "INVALID_INPUT", "message": "bad JSON"})

        ctx = context_from_request(request)
        task = body.get("task", "")
        k = body.get("k", 2)
        project = body.get("project")
        logger.debug(f"RecallHandler.handle: task={task!r}, k={k}, project={project!r}")

        try:
            results = self._recall.recall(task, k=k, project=project)
        except SonderError as e:
            return error_response(e)

        logger.debug(f"RecallHandler.handle: returning {len(results)} results")
        return Response(200, {"results": results})


class OutcomeHandler:
    """POST /v1/outcome — delegates to OutcomeService."""

    def __init__(self, outcome_service):
        self._outcome = outcome_service

    def handle(self, request: RequestLike) -> Response:
        logger.debug("OutcomeHandler.handle: incoming outcome request")
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("OutcomeHandler.handle: bad JSON in request body")
            return Response(400, {"error": "INVALID_INPUT", "message": "bad JSON"})

        interaction_id = body.get("interaction_id", "")
        signal = body.get("signal", "")

        if not interaction_id or not signal:
            logger.debug("OutcomeHandler.handle: missing interaction_id or signal")
            return Response(400, {"error": "INVALID_INPUT", "message": "interaction_id and signal required"})

        logger.debug(f"OutcomeHandler.handle: interaction_id={interaction_id!r}, signal={signal!r}")
        try:
            score = self._outcome.record(interaction_id, signal)
        except SonderError as e:
            return error_response(e)

        logger.debug(f"OutcomeHandler.handle: recorded score={score}")
        return Response(200, {"score": score})
