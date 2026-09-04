"""Pure HTTP authority mapping contract.

The HTTP boundary supplies runtime declarations explicitly.  Keeping this
small policy function free of root-module imports lets the served adapter
enforce role bindings without importing the broader server diagnostics
contract.
"""
from __future__ import annotations

import logging
logger = logging.getLogger(__name__)

SYSTEM_OPERATION_UNBOUND = "unbound-system-operation"


def system_operation_for(
    tool_name,
    *,
    operation_tools=None,
    operator_tools=None,
    canonicalize=None,
):
    name = str(tool_name or "").strip().lstrip("/")
    if not name:
        return ""
    if canonicalize is not None:
        name = canonicalize(name)
    operation_tools = {} if operation_tools is None else operation_tools
    operator_tools = frozenset() if operator_tools is None else operator_tools
    bound = operation_tools.get(name, "")
    if bound:
        logger.debug(f"system_operation_for: tool={name!r} -> bound operation={bound!r}")
        return str(bound)
    if name in operator_tools:
        logger.debug(f"system_operation_for: tool={name!r} -> unbound system operation")
        return SYSTEM_OPERATION_UNBOUND
    return ""


__all__ = ["SYSTEM_OPERATION_UNBOUND", "system_operation_for"]
