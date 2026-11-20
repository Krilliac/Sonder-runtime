"""Slash-command head folding in the REPL router is ASCII-only."""
from __future__ import annotations

import re

from sonder_runtime.interfaces.repl import command_router as cr


def test_master_head_is_not_unicode_folded():
    # U+212A (Kelvin sign) lowercases to "k"; an ASCII-only fold must not
    # turn "toKen" into the prose starter "token".
    match = re.match(
        r"^(?P<verb>orchestrate|master)\s+(?P<arg>.+)$",
        "master toKen budget is low",
    )
    assert cr._master_action(match) == "/master toKen budget is low"
