"""Opt-in machine-readable turn lines for piped/scripted REPL callers.

The piped REPL output contract is deliberately plain text (answer, then a
``[timing]`` line) and scripts already parse it, so it can never change by
default.  Some callers want structure instead of scraping -- one JSON object
per completed turn, in the spirit of the NDJSON event streams that agent
harnesses expose.  This module owns that opt-in contract:

* ``SONDER_REPL_NDJSON=1`` switches only the *piped* chat-result path to one
  NDJSON line per turn.  Interactive terminals keep their chrome: a human is
  not a JSON consumer, and the flag must not turn the console into one.
* The payload is versioned (``sonder.repl-turn.v1``) and additive-only, the
  same discipline as ``sonder.trace-span.v1``.  Consumers key on ``schema``.
* Lines are single-line, sorted-key, ASCII-safe JSON so any console encoding
  and any line-oriented reader (jq, findstr, PowerShell) can process them.

This is presentation packaging only.  It adds no fields the plain path did
not already print or hand back to the REPL (the answer text, error flag,
timing, feedback offer, and the interaction id the footer previously carried).
"""
from __future__ import annotations

import json

REPL_TURN_SCHEMA = "sonder.repl-turn.v1"
ENV_FLAG = "SONDER_REPL_NDJSON"


def enabled(environ) -> bool:
    """Whether the caller explicitly opted into NDJSON turn output."""
    try:
        value = (environ or {}).get(ENV_FLAG, "")
    except AttributeError:
        return False
    return str(value).strip() == "1"


def turn_payload(answer, *, elapsed_ms, error=False, interaction_id=None,
                 feedback_offered=False, label="Sonder") -> dict:
    """Build the stable ``sonder.repl-turn.v1`` mapping for one turn."""
    try:
        elapsed = max(0, int(elapsed_ms or 0))
    except (TypeError, ValueError, OverflowError):
        elapsed = 0
    identifier = str(interaction_id) if interaction_id else None
    return {
        "schema": REPL_TURN_SCHEMA,
        "label": str(label or "Sonder"),
        "answer": str(answer or ""),
        "error": bool(error),
        "elapsed_ms": elapsed,
        "interaction_id": identifier,
        "feedback_offered": bool(feedback_offered),
    }


def ndjson_line(payload) -> str:
    """Serialize one payload as a single sorted-key ASCII JSON line."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )


__all__ = ["ENV_FLAG", "REPL_TURN_SCHEMA", "enabled", "ndjson_line", "turn_payload"]
