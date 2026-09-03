"""Pure extraction of the programs an autopilot command list would run.

The autopilot ledger names only the lower-cased basename of each command's
program, never its arguments, and marks a malformed list as invalid rather
than guessing. Moved from ``server.py`` in the WP1 Three-Hundred-Twenty-Second
Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import json
import os


def command_programs(value) -> list[str]:
    if value in (None, ""):
        return []
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return ["(invalid)"]
    if isinstance(payload, dict):
        payload = payload.get("commands") or []
    if not isinstance(payload, list):
        return ["(invalid)"]
    programs = []
    for item in payload:
        command = item.get("cmd") if isinstance(item, dict) else item
        if not isinstance(command, list) or not command:
            return ["(invalid)"]
        programs.append(os.path.basename(str(command[0])).lower())
    return programs
