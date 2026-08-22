"""Pure message normalization policy for interactive control commands."""

from __future__ import annotations


def control_history_messages(history, prompt):
    """Keep supported conversation turns and append the current prompt.

    Control commands only need user/assistant text.  Ignore malformed history
    entries and empty content so callers cannot accidentally pass metadata or
    unrelated message roles to a downstream model request.
    """
    messages = []
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content") or ""
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    if prompt:
        messages.append({"role": "user", "content": prompt})
    return messages
