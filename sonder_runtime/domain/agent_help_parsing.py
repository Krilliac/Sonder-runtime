"""Extract tool names from agent help text blocks."""
from __future__ import annotations


def help_advertised_tools(help_text) -> tuple:
    """Tool names an agent help block advertises, one per '- name: {...}' line."""
    names = []
    for line in str(help_text or "").splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            continue
        name, separator, _ = stripped[2:].partition(":")
        name = name.strip()
        if separator and name.isidentifier():
            names.append(name)
    return tuple(names)
