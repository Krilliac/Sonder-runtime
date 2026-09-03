"""Pure parsing of an agent's JSON decision out of a model reply.

Small local models wrap decisions in markdown fences or surround them with
commentary. The parser is explicit-input and side-effect free: it takes the
raw reply text and returns the first complete top-level JSON object, raising
``ValueError`` when none is present so the decision-repair loop can
re-prompt. Moved from ``server.py`` in the WP1 Two-Hundred-Ninety-Seventh
Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import json


def extract_agent_json(text):
    """Parse an agent decision, tolerating markdown fences and prose framing.

    Small local models wrap decisions in ```json fences or surround them
    with commentary; a balanced-brace scan recovers the first complete JSON
    object instead of failing on trailing text. Genuinely truncated JSON
    still raises so the decision-repair loop can re-prompt.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        # Drop the opening fence line and any closing fence.
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        raise ValueError("agent response was not JSON: %s" % text[:300])
    # Balanced scan: find the first complete top-level object, ignoring
    # braces inside JSON strings, so prose after the object cannot break
    # parsing the way rfind("}") could.
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    break
    raise ValueError("agent response was not JSON: %s" % text[:300])
