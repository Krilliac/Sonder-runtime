"""Build byte-preserving synthesis inputs from producer-sealed fanout slots."""
from __future__ import annotations

import hashlib
import json

from sonder_runtime.adapters.fanout_receipt import synthesis_rows
from sonder_runtime.adapters.model_transport import ModelCallError


def sources(run, *, rows, load_prompt, max_source_chars):
    if run.get("status") != "completed":
        raise ModelCallError("configuration", "fanout run must be completed before synthesis")
    slots = synthesis_rows(run, rows)
    if sum(row.get("status") == "answered" for row, _ in slots) < 2:
        raise ModelCallError("configuration", "fanout synthesis requires at least two answered results")
    if sum(not error for _, error in slots) < 2:
        raise ModelCallError(
            "configuration",
            "fanout synthesis requires at least two validated-complete artifacts: " + "; ".join(
                "%s: %s" % (row["model"], error) for row, error in slots if error
            ),
        )
    original_prompt = load_prompt()
    source_rows, hashes = [], []
    for row, error in slots:
        if error:
            source_rows.append({"model": str(row["model"]), "status": "not_ready", "error": error})
            continue
        preview = row["answer"]
        source = {
            "model": str(row.get("model") or ""),
            "answer": preview,
            "elapsed_ms": row.get("elapsed_ms"),
            "answer_chars": row.get("answer_chars"),
            "stored_answer_chars": len(preview),
            "answer_truncated": bool(row.get("answer_truncated")),
            "thinking_chars": row.get("thinking_chars"),
            "done_reason": row.get("done_reason") or None,
        }
        source_rows.append(source)
        hashes.append({
            "model": source["model"],
            "preview_sha256": hashlib.sha256(preview.encode("utf-8")).hexdigest(),
        })
    try:
        bundle = json.dumps(
            {"question": original_prompt, "sources": source_rows},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ModelCallError("protocol", "fanout receipt cannot be serialized for synthesis") from exc
    if len(bundle) > max_source_chars:
        raise ModelCallError(
            "configuration", "fanout synthesis source exceeds %d characters; no sources were dropped"
            % max_source_chars,
        )
    return bundle, hashes
