"""Pure rendering policy for the active permission-mode context."""

from __future__ import annotations


def render_permission_mode_context(
    mode: str,
    labels,
    blurbs,
    ask_caveat: str,
    elevation_text: str,
) -> str:
    """Render mode and privilege context without reading or mutating state."""
    return "\n".join([
        "permission mode: %s -- %s" % (
            labels.get(mode, mode),
            blurbs.get(mode, ""),
        ),
        "  %s" % ask_caveat,
        elevation_text,
    ])


__all__ = ["render_permission_mode_context"]
