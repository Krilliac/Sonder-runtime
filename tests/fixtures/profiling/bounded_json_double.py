"""Test double for ``sonder_runtime.domain.common.bounded_json`` (owned by lane A).

Lane B (profiling) codes against the spec'd ``iter_array_objects`` interface.
Until lane A's module lands in the same tree, ``install()`` registers this
minimal, behaviour-compatible stand-in; once the real module exists it is used
unchanged and this double is inert.

Contract assumed from the spec:
``iter_array_objects(chunks, *, key="traceEvents", max_items, max_item_bytes=65_536)``
is a brace-matching scanner over text chunks that yields each decoded element
of the array under ``key``; oversize (or too deeply nested) elements are
skipped and counted. This double reports the count as the generator's return
value ``{"skipped": n, "truncated": bool}``.
"""
from __future__ import annotations

import json
import sys
import types

MAX_DEPTH = 64


def check_depth(text: str, max_depth: int = MAX_DEPTH) -> bool:
    depth = 0
    in_string = escape = False
    for char in text:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                return False
        elif char in "]}":
            depth -= 1
    return True


def loads_bounded(text: str, *, max_bytes: int, max_depth: int = MAX_DEPTH):
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("document too large")
    if not check_depth(text, max_depth):
        raise ValueError("document too deeply nested")
    try:
        return json.loads(text)
    except RecursionError:
        raise ValueError("document too deeply nested") from None


def iter_array_objects(chunks, *, key="traceEvents", max_items, max_item_bytes=65_536):
    """Yield elements of the array under ``key``; return skip/truncation stats."""
    marker = '"%s"' % key
    state = "seek"  # seek -> colon -> open -> items -> done
    buffer = ""
    skipped = 0
    items = 0
    element: list[str] = []
    element_size = 0
    depth = 0
    max_nesting = 0
    in_string = escape = False
    oversize = False
    top_in_string = top_escape = False
    top_depth = 0
    for chunk in chunks:
        for char in chunk:
            if state == "seek":
                # Track top-level position so only a key at depth 1 matches.
                if top_in_string:
                    buffer += char
                    if top_escape:
                        top_escape = False
                    elif char == "\\":
                        top_escape = True
                    elif char == '"':
                        top_in_string = False
                        if buffer == marker and top_depth == 1:
                            state = "colon"
                        buffer = ""
                    elif len(buffer) > len(marker) + 1:
                        buffer = buffer[-(len(marker) + 1):]
                    continue
                if char == '"':
                    top_in_string = True
                    buffer = '"'
                elif char in "[{":
                    top_depth += 1
                elif char in "]}":
                    top_depth -= 1
                continue
            if state == "colon":
                if char == ":":
                    state = "open"
                elif not char.isspace():
                    state = "seek"
                continue
            if state == "open":
                if char == "[":
                    state = "items"
                elif not char.isspace():
                    state = "seek"
                continue
            if state == "done":
                continue
            # state == "items"
            if depth == 0:
                if char.isspace() or char == ",":
                    continue
                if char == "]":
                    state = "done"
                    continue
                if char not in "{[":
                    # Scalars in the array are not objects: skip until a comma.
                    continue
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "{[":
                depth += 1
                max_nesting = max(max_nesting, depth)
            elif char in "}]":
                depth -= 1
            element_size += 1
            if element_size > max_item_bytes:
                oversize = True
                element = []
            elif not oversize:
                element.append(char)
            if depth == 0:
                if oversize or max_nesting > MAX_DEPTH:
                    skipped += 1
                else:
                    text = "".join(element)
                    try:
                        value = json.loads(text)
                    except (ValueError, RecursionError):
                        skipped += 1
                    else:
                        if items >= max_items:
                            return {"skipped": skipped, "truncated": True}
                        items += 1
                        yield value
                element = []
                element_size = 0
                max_nesting = 0
                oversize = False
    return {"skipped": skipped, "truncated": False}


def install() -> types.ModuleType:
    """Return the real bounded_json when present, else register this double."""
    try:
        from sonder_runtime.domain.common import bounded_json  # type: ignore[attr-defined]
        return bounded_json
    except ImportError:
        pass
    module = types.ModuleType("sonder_runtime.domain.common.bounded_json")
    module.check_depth = check_depth
    module.loads_bounded = loads_bounded
    module.iter_array_objects = iter_array_objects
    module.__doc__ = __doc__
    module.SONDER_TEST_DOUBLE = True
    sys.modules[module.__name__] = module
    import sonder_runtime.domain.common as common

    common.bounded_json = module
    return module
