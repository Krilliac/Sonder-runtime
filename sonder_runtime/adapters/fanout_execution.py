"""Execution-side helpers shared by plain fanout and batch child runs.

``server._execute_fanout_run`` stays one executor for both.  What differs for a
batch step -- a sealed system+prompt envelope, per-run sampling and context
settings, sealed full answers, and the token and served-model receipt -- lives
here as small explicit-input functions so the executor only calls them.
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, cast

from sonder_runtime.application.session.provider_attempts import (
    report_fanout_event,
    telemetry_error_code,
)
from sonder_runtime.domain.model_usage import usage_count
from sonder_runtime.domain.fanout_receipts import safe_answer as _default_redact
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

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


def emit_fanout_result(
    run_id: str,
    metadata: Mapping[str, Any],
    *,
    status: str = "ok",
    error_code: str | None = None,
) -> None:
    """Emit a content-free ``fanout.result`` event from a worker's metadata.

    Only bounded labels and counts cross the boundary; sealed answers and
    prompt/reply text are never inspected or exported.
    """
    usage = cast("Callable[[object], int | None]", usage_count)
    fields: dict[str, object] = {"status": status}
    served_model = metadata.get("served_model")
    if isinstance(served_model, str) and served_model:
        fields["served_model"] = served_model
    done_reason = metadata.get("done_reason")
    if isinstance(done_reason, str) and done_reason:
        fields["done_reason"] = done_reason
    tokens_in = usage(metadata.get("tokens_in"))
    if tokens_in is not None:
        fields["tokens_in"] = tokens_in
    tokens_out = usage(metadata.get("tokens_out"))
    if tokens_out is not None:
        fields["tokens_out"] = tokens_out
    if error_code:
        fields["error_code"] = error_code
    report_fanout_event("result", fields, run_id=run_id)


@contextlib.contextmanager
def fanout_lease_heartbeat(
    fanout_store: Any,
    run_id: str,
    owner_id: str,
    lease_seconds: float,
) -> Iterator[None]:
    """Keep a claimed fanout run's lease renewed while the worker is active."""
    stop = threading.Event()
    interval = max(5.0, float(lease_seconds) / 3.0)
    started = time.monotonic()

    def _loop() -> None:
        while not stop.wait(interval):
            with contextlib.suppress(Exception):
                fanout_store.heartbeat(run_id, owner_id, lease_seconds=lease_seconds)

    thread_ctor: Any = owned_runtime_thread
    thread = thread_ctor(
        target=_loop, daemon=True, name="sonder-fanout-heartbeat",
    )
    thread.start()
    report_fanout_event("started", {"status": "running"}, run_id=run_id)
    try:
        yield
    except BaseException as exc:
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        report_fanout_event(
            "failed",
            {
                "elapsed_ms": elapsed_ms,
                "status": "error",
                "error_code": telemetry_error_code(exc),
            },
            run_id=run_id,
        )
        raise
    else:
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        report_fanout_event(
            "completed",
            {"elapsed_ms": elapsed_ms, "status": "ok"},
            run_id=run_id,
        )
    finally:
        stop.set()
        thread.join(timeout=1.0)
