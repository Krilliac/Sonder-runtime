"""Approval-binding digest for debug plans.

``debug_command_digest`` hashes what an operator approves: the placeholder
argv of every step (never a nonce or run dir, which the launcher binds
later), the identity of each tool binary, the input's sha256, the engine
set, the network flag and the operator store ids. The evaluator and the
executor plan independently; equal requests over the same file therefore
produce equal digests, and any change to the file, the engine, the network
flag or a store changes it.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Sequence

from ..common.errors import InvalidInput


DIGEST_SCHEMA = "sonder.debug_command/1"


def _strings(values: Iterable, what: str) -> list[str]:
    out = []
    for value in values:
        if not isinstance(value, str):
            raise InvalidInput("%s entries must be strings" % what)
        out.append(value)
    return out


def debug_command_digest(template_argvs: Sequence[Sequence[str]], tool_identities: Sequence[str],
                         input_sha256: str, engines: Sequence[str], network: bool,
                         store_ids: Sequence[str] = ()) -> str:
    """64-hex sha256 over a canonical JSON encoding of the plan's identity."""
    payload = {
        "schema": DIGEST_SCHEMA,
        "argvs": [_strings(argv, "argv") for argv in template_argvs],
        "tools": _strings(tool_identities, "tool identity"),
        "input_sha256": str(input_sha256 or ""),
        "engines": _strings(engines, "engine"),
        "network": bool(network),
        "stores": _strings(store_ids, "store id"),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


__all__ = ["DIGEST_SCHEMA", "debug_command_digest"]
