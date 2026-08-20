"""Bounded diagnostic trace capture and rendering for completed turns."""
from __future__ import annotations

import collections
import time

_TURN_TRACES = collections.deque(maxlen=8)


def _capture_turn(model, tier, trace_ctx, prompt, response, iid=None):
    """Keep a bounded, in-memory trace of the most recent completed turns."""
    if not isinstance(trace_ctx, dict):
        return
    try:
        _TURN_TRACES.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": str(model or ""),
            "tier": str(tier or ""),
            "interaction_id": str(iid or ""),
            "prompt": str(prompt or "")[:4000],
            "augmented_prompt": str(trace_ctx.get("augmented_prompt") or "")[:16000],
            "lessons": [str(x)[:400] for x in (trace_ctx.get("lessons") or [])][:20],
            "facts_omitted": int(trace_ctx.get("facts_omitted") or 0),
            "response_head": str(response or "")[:2000],
        })
    except Exception:
        # Debug bookkeeping must never break the answer path it observes.
        pass


def _format_trace(model, tier, params, trace):
    lessons = trace.get("lessons", [])
    lines = [
        "",
        "=== TRACE (how Sonder Runtime decided) ===",
        "model: %s   tier: %s" % (model, tier),
        "generation params: %r" % (params,),
        "lessons retrieved: %d" % len(lessons),
    ]
    for lesson_text in lessons:
        lines.append("   - %s" % lesson_text)
    facts_omitted = int(trace.get("facts_omitted") or 0)
    if facts_omitted:
        lines.append("stored facts omitted by the block bound: %d" % facts_omitted)
    lines.append("--- exact prompt sent to the model ---")
    lines.append(trace.get("augmented_prompt", ""))
    lines.append("=== END TRACE ===")
    return "\n".join(lines)
