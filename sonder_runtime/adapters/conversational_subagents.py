"""Request-bound real model runner for the legacy durable child provider."""

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
import time
import uuid
from ..application.ports.model_gateway import ModelRequest, require_model_text


def conversational_runner_factory(gateway, sessions, capture):
    def bind(request, context):
        session_id = "child-session-" + (request.child_id or uuid.uuid4().hex)

        def run(state, save, control):
            # An admitted call with no terminal receipt cannot be safely repeated.
            history_events = sessions.read_range(session_id, limit=1000)
            pending_ids = {
                e.payload.get("request_id")
                for e in history_events
                if e.event_type == "model.requested"
            }
            terminal_ids = {
                e.payload.get("request_id")
                for e in history_events
                if e.event_type in {"model.response", "model.failed"}
            }
            if pending_ids - terminal_ids:
                raise RuntimeError(
                    "unresolved child model attempt requires reconciliation"
                )
            metadata = dict(request.metadata)
            roots = tuple(
                Path(p).resolve()
                for p in metadata.get("workspace_read_roots", "").split("|")
                if p
            )
            if roots and (
                not context.workspace_roots
                or not all(
                    any(
                        root == p.resolve() or p.resolve() in root.parents
                        for p in context.workspace_roots
                    )
                    for root in roots
                )
            ):
                raise PermissionError("child workspace exceeds inherited authority")
            seconds = request.budget.max_wall_seconds or 120
            deadline = time.monotonic() + seconds
            if context.deadline_monotonic is not None:
                deadline = min(deadline, context.deadline_monotonic)
            child_context = replace(
                context,
                workspace_roots=roots or context.workspace_roots,
                cancellation=control,
                deadline_monotonic=deadline,
            )
            model_request = ModelRequest(
                request.prompt,
                tier=metadata.get("tier", "code"),
                history=tuple(state.get("history", ())),
                options={"num_predict": request.budget.max_output_tokens or 2048},
            )
            request_id = "child-request-" + uuid.uuid4().hex
            pending = capture.begin_request(
                session_id,
                request_id,
                model_request,
                request_id=request_id,
                user_message=request.prompt,
            )
            try:
                from ..application.session.provider_attempts import (
                    provider_attempt_scope,
                )

                scope = provider_attempt_scope(capture, pending)
            except ImportError:
                scope = nullcontext()
            with scope:
                response = gateway.generate(model_request, child_context)
            text = require_model_text(response.text)
            capture.complete_request(pending, model_response=text)
            save(
                {
                    "session_id": session_id,
                    "history": list(model_request.history)
                    + [
                        {"role": "user", "content": request.prompt},
                        {"role": "assistant", "content": text},
                    ],
                },
                request_id,
            )
            return text

        return run

    return bind
