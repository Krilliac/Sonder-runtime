"""Rendering for crash reports, profile digests and debug runs.

Interfaces (REPL, HTTP) may import only application and interfaces, so they
render the lane A/B domain results through these functions instead of the
domain renderers. The domain modules are imported when first used: this
module stays importable in a runtime composed without the readers, where the
debug tools report themselves unavailable.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .ports import DebugPlan, DebugRunOutcome

MAX_WIRE_BYTES = 48_000
_ENVELOPE_RESERVE = 4_000


def report_to_wire(report, max_bytes: int = MAX_WIRE_BYTES) -> dict:
    from ...domain.crash.render import report_to_wire as render

    return render(report, max_bytes=max_bytes)


def render_report(report, max_chars: int = 12_000) -> str:
    from ...domain.crash.render import render_report as render

    return render(report, max_chars=max_chars)


def digest_to_wire(digest, max_bytes: int = MAX_WIRE_BYTES) -> dict:
    from ...domain.profiling.render import digest_to_wire as render

    return render(digest, max_bytes=max_bytes)


def render_digest(digest, max_chars: int = 12_000) -> str:
    from ...domain.profiling.render import render_digest as render

    return render(digest, max_chars=max_chars)


def render_bucket_table(buckets: Sequence[Any], max_chars: int = 8_000) -> str:
    from ...domain.crash.render import render_bucket_table as render

    return render(buckets, max_chars=max_chars)


def buckets_to_wire(buckets: Sequence[Any]) -> list[dict]:
    if not buckets:
        return []
    from ...domain.crash.render import bucket_to_wire

    return [bucket_to_wire(bucket) for bucket in buckets]


def _size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                          default=str).encode("utf-8"))


def outcome_to_wire(outcome: DebugRunOutcome, max_bytes: int = MAX_WIRE_BYTES) -> dict:
    """``{"object": "debug_run", ...}`` with the report or digest fitted inside ``max_bytes``."""
    payload: dict[str, Any] = {
        "object": "debug_run",
        "run_id": outcome.run_id,
        "status": outcome.status,
        "engines": list(outcome.engines),
        "command_digest": outcome.command_digest,
        "network": bool(outcome.network),
        "egress_isolation": outcome.egress_isolation,
        "staging": outcome.staging,
        "notes": list(outcome.notes),
    }
    if outcome.error_code:
        payload["error_code"] = outcome.error_code
    if outcome.display_argvs:
        payload["display_argvs"] = [list(item) for item in outcome.display_argvs]
    if outcome.status == "running":
        payload["next"] = "call debug_run_result with this run_id to wait for the result"
    budget = max(4_000, int(max_bytes) - _ENVELOPE_RESERVE - _size(payload))
    if outcome.crash is not None:
        payload["crash"] = report_to_wire(outcome.crash, max_bytes=budget)
    if outcome.profile is not None:
        payload["profile"] = digest_to_wire(outcome.profile, max_bytes=budget)
    if outcome.buckets is not None:
        payload["buckets"] = buckets_to_wire(outcome.buckets)
    while _size(payload) > max_bytes and payload.get("notes"):
        payload["notes"] = payload["notes"][: len(payload["notes"]) // 2]
        payload["truncated"] = True
    while _size(payload) > max_bytes and payload.get("display_argvs"):
        payload["display_argvs"] = payload["display_argvs"][: len(payload["display_argvs"]) // 2]
        payload["truncated"] = True
    while _size(payload) > max_bytes and payload.get("buckets"):
        payload["buckets"] = payload["buckets"][: len(payload["buckets"]) // 2]
        payload["truncated"] = True
    return payload


def render_outcome(outcome: DebugRunOutcome, max_chars: int = 12_000) -> str:
    """Console text for a run: status line, then the report or digest."""
    head = "debug run %s: %s" % (outcome.run_id or "(pure)", outcome.status)
    if outcome.error_code:
        head += " [%s]" % outcome.error_code
    if outcome.engines:
        head += "  engines: %s" % ", ".join(outcome.engines)
    lines = [head]
    if outcome.egress_isolation and outcome.egress_isolation != "n/a":
        lines.append("egress isolation: %s" % outcome.egress_isolation)
    budget = max(200, max_chars - 400)
    if outcome.crash is not None:
        lines.append(render_report(outcome.crash, max_chars=budget))
    elif outcome.profile is not None:
        lines.append(render_digest(outcome.profile, max_chars=budget))
    elif outcome.buckets is not None:
        lines.append(render_bucket_table(outcome.buckets, max_chars=budget))
    if outcome.crash is None and outcome.profile is None:
        for note in outcome.notes[:8]:
            lines.append("note: %s" % note)
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def render_resolved_command(plan: DebugPlan) -> str:
    """What the console shows before an operator types ``y`` for ``--symbols-online``."""
    command = plan.resolved_command()
    lines = [
        "input: %s" % command["input_label"],
        "input sha256: %s" % command["input_sha256"],
        "engines: %s" % ", ".join(command["engines"]),
        "network: %s" % ("yes (symbol servers)" if command["network"] else "no"),
        "isolation: %s" % command["isolation"],
    ]
    for store in command["stores_display"]:
        lines.append("store: %s" % store)
    for argv in command["display_argvs"]:
        lines.append("$ " + " ".join(argv))
    lines.append("command digest: %s" % command["command_digest"])
    return "\n".join(lines)


__all__ = [
    "MAX_WIRE_BYTES", "buckets_to_wire", "digest_to_wire", "outcome_to_wire", "render_bucket_table",
    "render_digest", "render_outcome", "render_report", "render_resolved_command", "report_to_wire",
]
