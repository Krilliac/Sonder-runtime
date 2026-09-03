"""Normalization of an offload ``schema`` argument into a schema object.

Internal callers pass a parsed object and the tool surface passes JSON text;
anything that is not a JSON object is a typed configuration failure rather
than a silently unconstrained call. It raises the transport's
``ModelCallError``, so it lives with the adapters. Moved from ``server.py``
in the WP1 Three-Hundred-Nineteenth Slice with its behaviour byte-for-byte
intact.
"""
from __future__ import annotations

import json

from sonder_runtime.adapters.model_transport import ModelCallError


def parse_schema_arg(schema):
    """Normalize an offload `schema` argument to a schema object, or None.

    Accepts an already-parsed object (internal callers) or the JSON text the
    tool surface passes (matching how every other structured argument crosses
    that boundary). A blank string means "no schema", so the unconstrained path
    stays the default. Anything else that is not a JSON object is a caller
    error and is raised as a typed configuration failure -- never quietly
    dropped, because dropping it would run the call unconstrained while the
    caller believed it was constrained.
    """
    if schema is None:
        return None
    if isinstance(schema, dict):
        return schema
    if isinstance(schema, str):
        text = schema.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ModelCallError(
                "configuration",
                "schema is not valid JSON: %s" % exc,
            ) from exc
        if not isinstance(parsed, dict):
            raise ModelCallError(
                "configuration",
                "schema must be a JSON object, got %s" % type(parsed).__name__,
            )
        return parsed
    raise ModelCallError(
        "configuration",
        "schema must be a JSON object or JSON text, got %s" % type(schema).__name__,
    )
