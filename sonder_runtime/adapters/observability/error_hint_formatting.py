"""Actionable next-step hints for known REPL error shapes.

The interactive console renders a failed turn verbatim inside the error
panel.  For a handful of *known* failure shapes there is one obvious next
command, and asking the user to reverse-engineer it from the raw error is
avoidable friction.  This module maps those shapes to a single short hint.

Rules that keep this from becoming a second error system:

* Every pattern is grounded in a message another module actually emits
  (``cloud_access.cloud_disabled_message``, ``model_error_formatting``,
  the REPL's model-pin refusals, ``_gate_tools`` refusals).  Nothing here
  guesses at free-form model output.
* A miss returns ``""``.  The hint is presentation-only garnish: it never
  rewrites, classifies, or suppresses the error it accompanies, and the
  piped/scripted output contract never includes it.
* Hints name commands and switches that exist today.  A hint that points
  at a control which was renamed is worse than no hint, so each pattern's
  test re-asserts the trigger literal against the emitting module.
"""
from __future__ import annotations


def error_hint(text) -> str:
    """Return one short next-step hint for a known error, or ``""``.

    The return value is bare prose without a ``hint:`` prefix or styling;
    the console owns presentation.
    """
    value = str(text or "")

    # cloud_access.cloud_disabled_message() -- the message already names the
    # opt-in switch, so the hint offers the local alternative instead.
    if "hosted/cloud tiers are disabled" in value:
        return "/model lists local tiers you can switch to without cloud opt-in."

    # The REPL's own pin refusals (see _is_repl_error): the pinned tag
    # disappeared or cannot serve the selected route.
    if "model pin '" in value:
        if " is unavailable or is not chat-capable." in value:
            return (
                "/model lists installed chat-capable models and tiers; "
                "pick one to re-pin."
            )
        if "' is incompatible with the selected " in value:
            return "/model <tier> selects a tier and clears the exact-model pin."

    # model_error_formatting.format_runtime_model_call_error() targets.
    if "ERROR contacting local Ollama" in value:
        return (
            "local Ollama did not answer -- make sure it is running "
            "(ollama serve, or the sonder headless launcher), then retry."
        )
    if "ERROR contacting remote Ollama" in value:
        return (
            "check OLLAMA_HOST and that the remote endpoint is reachable; "
            "remote Ollama also requires SONDER_ALLOW_REMOTE_OLLAMA=1."
        )
    if "ERROR contacting hosted Ollama" in value:
        return "the hosted endpoint did not answer; check network and cloud configuration."

    # model_error_formatting.format_model_call_error(kind="http").
    if "rejected the model request (HTTP 404" in value:
        return (
            "the endpoint answered but knows no such model -- /model lists "
            "installed tags; `ollama pull <tag>` installs one."
        )
    for status in ("408", "429", "502", "503", "504"):
        if "rejected the model request (HTTP %s" % status in value:
            return "HTTP %s is usually transient; retry shortly." % status

    # _gate_tools() refusal: "refused <cmd>: <reason> (mode: <mode>)".
    if value.startswith("refused ") and "(mode: " in value:
        if "(mode: plan)" in value:
            return (
                "/permissions changes the mode · "
                "/mode manual asks per action instead"
            )
        return (
            "/permissions changes the mode · "
            "a rule can allow specific paths"
        )

    return ""


__all__ = ["error_hint"]
