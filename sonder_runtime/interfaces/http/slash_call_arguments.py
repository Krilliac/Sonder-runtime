"""Tool arguments a file-changing slash command calls with."""
from __future__ import annotations


def slash_call_arguments(cmd: str, argument: object) -> dict[str, str] | None:
    """The tool arguments a file-changing slash command will call with, or None.

    Mirrors the branches of ``_handle_slash`` exactly (``/write`` and
    ``/append`` call ``file_write``, ``/edit`` calls ``file_edit``), so an
    unattended refusal names the call (a call id an operator can approve once)
    and the same call digests identically whether it is typed ``/write a b``
    or ``/file_write path=a content=b mode=create``. The caller's token is a
    credential knob and never part of the digest.
    """
    text = str(argument or "")
    if cmd in ("/write", "/append"):
        parts = text.split(None, 1)
        if len(parts) != 2:
            return None
        return {"path": parts[0], "content": parts[1],
                "mode": "append" if cmd == "/append" else "create"}
    if cmd == "/edit":
        pieces = text.split("|", 2)
        if len(pieces) != 3:
            return None
        return {"path": pieces[0].strip(), "old": pieces[1], "new": pieces[2]}
    return None
