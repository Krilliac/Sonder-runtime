"""Generation of one structurally valid agent decision with bounded repair.

The agent loop asks the model for a JSON decision; malformed or truncated
replies get a bounded number of host format-repair prompts, transport
failures come back as ordinary outcomes and cancellation keeps its exception
identity. It catches the transport's ``ModelCallError``, so it lives with the
adapters. Moved from ``server.py`` in the WP1 Three-Hundred-Twenty-Third
Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.agents.decision_parsing import extract_agent_json


DECISION_REPAIR_LIMIT = 2


def generate_decision(
    gen,
    step_prompt,
    repair_limit=DECISION_REPAIR_LIMIT,
    require_final=False,
    *,
    write_chunk_hint,
):
    """Generate one structurally valid agent decision with bounded format repair.

    ``write_chunk_hint`` is the character budget named in the length-recovery
    instruction; it is injected because the hosted agent budgets stay with
    the agent loop.
    """
    repair_limit = max(0, min(4, int(repair_limit)))
    length_limited = False
    try:
        raw = gen(step_prompt)
    except ModelCallError as error:
        # Cancellation is a control-flow signal used by fleet callers and must
        # retain its stable exception identity.  Hosted/local transport
        # failures, however, are ordinary agent outcomes: return them to the
        # agent boundary instead of leaking an MCP traceback.
        if error.kind == "cancelled":
            raise
        length_limited = (
            error.kind == "empty_response"
            and '"done_reason": "length"' in error.detail
            and repair_limit > 0
        )
        if not length_limited:
            return None, "", error
        raw = ""
    length_limited = length_limited or (
        getattr(gen, "last_response_meta", {}).get("done_reason") == "length"
    )
    error = None
    for attempt in range(repair_limit + 1):
        try:
            decision = extract_agent_json(raw)
            if not isinstance(decision, dict):
                raise ValueError("agent decision must be a JSON object")
            if require_final and "final" not in decision:
                raise ValueError("agent finalization response must contain 'final'")
            if not require_final and "final" not in decision and not decision.get("tool"):
                raise ValueError("agent decision omitted both 'tool' and 'final'")
            return decision, raw, None
        except Exception as exc:
            error = exc
        if attempt >= repair_limit:
            break
        valid_shape = (
            '{"final":"answer"}'
            if require_final else
            '{"tool":"name","args":{},"reason":"brief"} or {"final":"answer"}'
        )
        recovery = ""
        if length_limited:
            recovery = (
                "\nHOST LENGTH RECOVERY: the previous decision hit its output "
                "limit. Do not repeat the oversized response. For a large "
                "file_write, emit one valid chunk of at most %d characters now "
                "and use append on a later tool turn; otherwise simplify the "
                "decision."
                % write_chunk_hint
            )
        repair_prompt = (
            step_prompt
            + "\n\nHOST FORMAT REPAIR %d/%d: Your previous response was invalid. "
            "Return exactly one JSON object and no prose or Markdown. Use %s.\n"
            "Parser error: %s%s\nPrevious response excerpt:\n%s"
            % (
                attempt + 1,
                repair_limit,
                valid_shape,
                error,
                recovery,
                str(raw or "")[:1000],
            )
        )
        try:
            raw = gen(repair_prompt)
            length_limited = (
                getattr(gen, "last_response_meta", {}).get("done_reason")
                == "length"
            )
        except ModelCallError as model_error:
            if model_error.kind == "cancelled":
                raise
            if (
                model_error.kind == "empty_response"
                and '"done_reason": "length"' in model_error.detail
                and attempt + 1 < repair_limit
            ):
                length_limited = True
                raw = ""
                continue
            return None, raw, model_error
    return None, raw, error
