"""Authenticated HTTP glue for durable lanes and background-work discovery.

The serving host injects identity, scope, lifecycle and status providers. This
keeps the legacy handler small without importing the root server here.
"""
from __future__ import annotations

import sys

from ...application.errors import DependencyUnavailable, InvalidInput, SonderError
from ..agent_lane_entrypoint import http_parent_scope
from ..orchestration_commands import CommandReply
from .delegate import dispatch_delegate


def _scope_payload(payload, query, principal):
    if payload is not None and not isinstance(payload, dict):
        raise InvalidInput("request must be an object")
    payload = dict(payload or {})
    for fields in (payload, query):
        if "parent_session_id" in fields:
            fields["parent_session_id"] = http_parent_scope(fields["parent_session_id"], principal)
    return payload


def _authorize(policy, method, path, payload, query, context, *, surface="http"):
    if method == "GET":
        return
    arguments = {
        "method": method, "path": path, "payload": payload,
        "query": query, "principal_id": context.principal_id,
        "workspace_roots": [str(root) for root in context.workspace_roots],
    }
    decision = policy.decide_for_caller(
        "agent_lane", interactive=False, gate_control_exempt=False,
        surface=surface, arguments=arguments,
    )
    if decision is not None and decision.action != policy.allow_action():
        raise PermissionError(decision.reason)


def _service(application):
    factory = getattr(application, "agent_lanes", None)
    if not callable(factory):
        raise DependencyUnavailable("agent conversations are unavailable")
    return factory()


def _state_home(application, state_home_of=None):
    if state_home_of is not None:
        value = state_home_of(application)
        if value:
            return value
    state = getattr(getattr(application, "config", None), "state", None)
    value = getattr(state, "home", None)
    if value:
        return value
    raise DependencyUnavailable("state home is unavailable")


def delegate_chat(task, *, application, context, project, policy,
                  parent_session_id="", command_id="", surface="http",
                  allow_creation=False, state_home_of=None):
    """The chat spelling executes the same authorized lane mutation as HTTP."""
    if not task.strip():
        return "usage: /delegate <task>"
    payload = {"task": task, "project": project}
    if parent_session_id:
        payload["parent_session_id"] = parent_session_id
    if command_id:
        payload["command_id"] = command_id
    try:
        payload = _scope_payload(payload, {}, context.principal_id)
        _authorize(
            policy, "POST", "/v1/agent-lanes/delegate", payload, {}, context,
            surface=surface,
        )
        receipt = dispatch_delegate(_service(application), payload, context,
                                    state_home=_state_home(application, state_home_of),
                                    allow_creation=allow_creation)
        lane = receipt["delegation"]
        return CommandReply(f"Agent {lane['lane_id']} started in {lane['folder']}.", agent_lane=lane)
    except (SonderError, ValueError, TypeError, PermissionError) as error:
        return f"Could not delegate: {error}"
    finally:
        policy.forget_spent_approval()


def native_delegate_reply(task, *, context_of, project="", policy,
                          parent_session_id="", state_home_of=None):
    """Native chat adapter for ``/delegate`` using the same lane mutation."""
    if not task.strip():
        return "usage: /delegate <task>"
    try:
        application, context = context_of()
        return delegate_chat(
            task, application=application, context=context, project=project,
            policy=policy, parent_session_id=parent_session_id,
            surface="native", allow_creation=True, state_home_of=state_home_of,
        )
    except (SonderError, ValueError, TypeError, PermissionError) as error:
        return f"Could not delegate: {error}"


def handle_agent_work_request(handler, method, path, payload, *, application_of,
                              context_of, background_of, policy, logger,
                              query=None, query_of=None, state_home_of=None,
                              allow_creation_of=None):
    if path != "/v1/background-work" and path != "/v1/agent-lanes" and not path.startswith("/v1/agent-lanes/"):
        return False
    auth = handler._request_auth_context()
    if not auth.get("authorized"):
        handler._send_auth_error()
        return True
    try:
        application = application_of()
        if query is None and query_of is not None:
            query = query_of(handler)
        query = dict(query or {})
        if any(isinstance(values, (list, tuple)) and len(values) != 1
               for values in query.values()):
            raise InvalidInput("query fields must occur only once")
        query = {
            key: (values[0] if isinstance(values, (list, tuple)) else values)
            for key, values in query.items()
        }
        context = context_of(auth, handler._correlation())
        payload = _scope_payload(payload, query, context.principal_id)
        if path == "/v1/background-work":
            from .background_work import dispatch_background_work_route
            result = dispatch_background_work_route(
                background_of(application, auth), method, path, context=context, query=query,
            )
        else:
            # Resolve the facade at request time.  Besides keeping the
            # boundary injectable, this preserves test and embedding hosts
            # that replace the facade with a scoped implementation.
            lane_facade = sys.modules.get(
                "sonder_runtime.interfaces.http.facades.agent_lanes"
            )
            if lane_facade is None:
                from .facades import agent_lanes as lane_facade
            _authorize(policy, method, path, payload, query, context)
            if path == "/v1/agent-lanes/delegate":
                result = lane_facade.AgentLaneHttpResult(
                    {"error": "METHOD_NOT_ALLOWED"}, 405
                )
                if method == "POST":
                    result = lane_facade.AgentLaneHttpResult(dispatch_delegate(
                        _service(application), payload, context,
                        state_home=_state_home(application, state_home_of),
                        allow_creation=(
                            bool(allow_creation_of(auth))
                            if allow_creation_of is not None
                            else context.principal_id in {"owner", "local-owner", "local"}
                        ),
                    ), 202)
            else:
                result = lane_facade.dispatch_agent_lane_route(
                    _service(application), method, path, payload, query, context
                )
        if result is None:
            handler._send_not_found()
        else:
            handler._send_json_payload(result.body, status=result.status_code)
    except (SonderError, ValueError, TypeError, PermissionError) as error:
        code = "FORBIDDEN" if isinstance(error, PermissionError) else getattr(error, "code", "INVALID_INPUT")
        status = {"UNAUTHENTICATED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404,
                  "CONFLICT": 409, "CONCURRENCY_CONFLICT": 409, "CAPACITY_EXCEEDED": 429,
                  "DEPENDENCY_UNAVAILABLE": 503, "INTEGRITY_FAILURE": 503,
                  "INTERNAL_FAILURE": 503, "DEADLINE_EXCEEDED": 408, "CANCELLED": 409}.get(code, 400)
        handler._send_json_payload(
            {"error": {"code": code, "message": str(error), "type": "agent_lane_error"}}, status=status,
        )
    except Exception:
        logger.exception("agent work request failed, correlation=%r", handler._correlation())
        handler._send_json_payload(
            {"error": {"code": "DEPENDENCY_UNAVAILABLE", "message": "agent conversations are unavailable",
                       "type": "server_error"}}, status=503,
        )
    finally:
        policy.forget_spent_approval()
    return True
