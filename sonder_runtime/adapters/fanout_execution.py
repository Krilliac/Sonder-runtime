"""Execution-side helpers shared by plain fanout and batch child runs.

``server._execute_fanout_run`` stays one executor for both.  What differs for a
batch step -- a sealed system+prompt envelope, per-run sampling and context
settings, sealed full answers, and the token and served-model receipt -- lives
here as small explicit-input functions so the executor only calls them.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, cast

from sonder_runtime.domain.model_usage import usage_count
from sonder_runtime.domain.fanout_receipts import safe_answer as _default_redact

ENVELOPE_VERSION = 1


def seal_envelope(system: str, prompt: str) -> str:
    """The plaintext a batch child run seals: system and prompt together."""
    return json.dumps(
        {"v": ENVELOPE_VERSION, "system": str(system or ""), "prompt": str(prompt or "")},
        ensure_ascii=False, separators=(",", ":"),
    )


def fanout_payload(limits: Mapping[str, Any], plaintext: str) -> tuple[str, str]:
    """Return ``(question, system)`` from the decrypted sealed payload.

    A plain run seals the raw prompt string.  A batch child run seals the JSON
    envelope above; a malformed one raises ``ValueError`` so the caller fails
    the claimed rows exactly as it does for an unreadable vault token.
    """
    if not limits.get("batch_job_id"):
        return plaintext, ""
    try:
        envelope = json.loads(plaintext)
    except (TypeError, ValueError) as exc:
        raise ValueError("sealed batch payload is malformed") from exc
    if (
        not isinstance(envelope, dict) or envelope.get("v") != ENVELOPE_VERSION
        or not isinstance(envelope.get("prompt"), str) or not isinstance(envelope.get("system"), str)
    ):
        raise ValueError("sealed batch payload is malformed")
    return envelope["prompt"], envelope["system"]


def generate_extras(limits: Mapping[str, Any]) -> dict[str, Any]:
    """Optional ``_make_generate`` keywords; a plain run passes none."""
    return {} if limits.get("think") is None else {"think": limits["think"]}


def call_metadata(generate: object) -> dict[str, Any]:
    """Bounded response metadata and token usage from a finished (or failed) call.

    ``generate`` may be ``None`` when building it failed.  Usage is copied only
    when the call reported some: a failed call has none, and its tokens stay
    unknown rather than becoming 0.
    """
    meta = getattr(generate, "last_response_meta", None)
    out = dict(meta) if isinstance(meta, dict) else {}
    usage = getattr(generate, "last_usage", None)
    if isinstance(usage, dict) and usage:
        out["tokens_in"] = usage.get("tokens_in")
        out["tokens_out"] = usage.get("tokens_out")
        out["token_source"] = usage.get("token_source", "")
    return out


def stored_answer(
    limits: Mapping[str, Any], raw_answer: str, question: str, metadata: dict[str, Any], *,
    seal: Callable[[str], str], redact: Callable[[str, str], str] = _default_redact,
) -> str:
    """The answer text for the durable row.

    A plain run keeps the prompt-echo and credential redaction.  A sealed run
    (batch child) stores nothing in plaintext: the full answer, bounded by
    ``answer_max_chars``, goes into ``metadata`` as a vault token so the text
    stays exact for later stages and for the owner.
    """
    if not limits.get("sealed_answers"):
        return redact(raw_answer, question)
    kept = raw_answer[:limits["answer_max_chars"]]
    metadata["sealed_answer"] = seal(kept)
    metadata["sealed_truncated"] = len(kept) < len(raw_answer)
    return ""


def result_fields(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """``fanout_store.record_result`` keywords carried by a call's metadata."""
    usage = cast("Callable[[object], int | None]", usage_count)
    return {
        "answer_chars": metadata.get("answer_chars"),
        "thinking_chars": metadata.get("thinking_chars"),
        "done_reason": metadata.get("done_reason", ""),
        "tokens_in": usage(metadata.get("tokens_in")),
        "tokens_out": usage(metadata.get("tokens_out")),
        "token_source": metadata.get("token_source", ""),
        "served_model": metadata.get("served_model", ""),
        "sealed_answer": metadata.get("sealed_answer", ""),
        "sealed_truncated": bool(metadata.get("sealed_truncated")),
    }
