"""Chat-surface adapter for the gated computer-use agent route."""
from __future__ import annotations

import json

from sonder_runtime.domain.computer_use.intent import classify
from sonder_runtime.domain.cloud_access import LEGACY_ERROR_PREFIX, has_legacy_error_prefix


def _dispatch(server, name, args):
    """Invoke one fixed computer tool under the same managed admission as agents."""
    with server._managed_agent_admission_scope():
        refusal = server._agent_run_tool_refusal(name, allow_web=False)
        if refusal:
            return f"{LEGACY_ERROR_PREFIX} HOST POLICY: {refusal}"
        return server._agent_dispatch(name, args, allow_web=False, read_only=False)


def _status_guidance(status_text):
    if has_legacy_error_prefix(status_text):
        return status_text
    try:
        status = json.loads(status_text)
    except (TypeError, ValueError):
        return "Computer use status could not be read safely: %s" % status_text
    if not isinstance(status, dict) or status.get("ok") is not True:
        return "Computer use status could not be read safely: %s" % status_text
    if not status.get("enabled") or not status.get("allowed_apps"):
        return (
            "Computer use is disabled. Set [computer_use] enabled = true and "
            "configure a non-empty allowed_apps list."
        )
    session = status.get("session") or {}
    if not isinstance(session, dict) or session.get("active") is not True:
        return (
            "Computer use is enabled, but no driving session is live. Open an "
            "allowlisted app, call computer_use_start with its app or hwnd, "
            "approve that pending call with /approve <id> at the Sonder console, "
            "then retry the identical start call. Repeat your request after it starts."
        )
    return ""


def route(text: str, server) -> str | None:
    """Run a recognized computer request through fixed, gated host dispatch.

    ``server`` is the legacy runtime, passed in by its caller: packaged code
    never imports it (tests/test_wp1_root_server_boundary.py).
    """
    intent = classify(text)
    if intent is None:
        return None
    if intent.tool == "computer_use_stop":
        return str(_dispatch(server, intent.tool, intent.args))
    status = str(_dispatch(server, "computer_use_status", {}))
    guidance = _status_guidance(status)
    if guidance:
        return guidance
    return str(_dispatch(server, intent.tool, intent.args))
