"""Compare-and-swap persistence of a verified code repair.

A captured broken response is replaced by its repaired version only while
the interaction's learning state is unchanged, with the repair's token
usage added and its provenance recorded. It writes the memory database, so
it lives with the adapters. Moved from ``server.py`` in the WP1
Three-Hundred-Thirty-Third Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import sonder_runtime.adapters.memory_store as memory_store


def persist_verified_code_repair(
    interaction_id, expected, repaired_response, repair_usage, *, open_db,
):
    """Replace a captured broken response only while its learning state is unchanged.

    ``open_db()`` opens the memory database; it is injected so the root
    delegate keeps the database seam.
    """
    if not interaction_id or not expected or not isinstance(repair_usage, dict):
        return False
    try:
        repair_tokens_in = int(repair_usage["tokens_in"])
        repair_tokens_out = int(repair_usage["tokens_out"])
        original_tokens_in = int(expected.get("tokens_in") or 0)
        original_tokens_out = int(expected.get("tokens_out") or 0)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if min(
        repair_tokens_in, repair_tokens_out,
        original_tokens_in, original_tokens_out,
    ) < 0:
        return False
    original_source = str(expected.get("token_source") or "").strip().lower()
    repair_source = str(repair_usage.get("token_source") or "").strip().lower()
    if original_source == repair_source == "ollama":
        token_source = "ollama+code-repair"
    elif original_source == repair_source == "estimated":
        token_source = "estimated+code-repair"
    else:
        token_source = "mixed+code-repair"
    try:
        conn = open_db()
        try:
            return memory_store.replace_interaction_response_cas(
                conn,
                interaction_id,
                expected=expected,
                response=repaired_response,
                tokens_in=original_tokens_in + repair_tokens_in,
                tokens_out=original_tokens_out + repair_tokens_out,
                token_source=token_source,
            )
        finally:
            conn.close()
    except Exception:
        return False
