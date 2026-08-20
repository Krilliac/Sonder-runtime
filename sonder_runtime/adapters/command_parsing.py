"""Small parsers shared by the runtime command interfaces."""
from __future__ import annotations

def _parse_game_campaign_command(arg: str) -> dict | None:
    parts = [part.strip() for part in str(arg or "").split("|", 3)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    kwargs = {"name": parts[0], "concept": parts[1]}
    if len(parts) > 2 and parts[2]:
        kwargs["language"] = parts[2]
    if len(parts) > 3 and parts[3]:
        kwargs["dimension"] = parts[3]
    return kwargs
