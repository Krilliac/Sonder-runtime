"""Natural-language run/train intents take the gate of the slash command they name.

"run it" is ``/run`` and "practice 3 tasks" is ``/train 3``. A surface that maps
such a phrase to an action dispatches that slash line through its own gated
slash handler instead of calling the runner, so permission mode, deny rules and
role checks apply exactly as they do to the typed command; calling the runner
directly let ``plan`` and unattended ``manual`` run a code block that ``/run``
refuses. A caller without developer authority is refused before any dispatch,
and an authority check that raises on a malformed context counts as refused.

Stdlib only; no state.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

DEVELOPER_REQUIRED = "refused %s: developer or admin authentication is required"


def slash_commands(intent: Mapping[str, object]) -> list[str]:
    """The slash lines an intent's run and train actions stand for, run first."""
    commands = ["/run"] if intent.get("run") else []
    train = intent.get("train")
    if isinstance(train, int) and not isinstance(train, bool):
        commands.append(f"/train {train}")
    return commands


def dispatch(
    intent: Mapping[str, object],
    slash: Callable[[str], str],
    authorized: Callable[[], bool],
) -> list[str]:
    """Reply text for each slash line *intent* stands for, in order.

    *slash* is the surface's gated slash handler; *authorized* says whether the
    caller holds developer authority and is asked once, only when there is a
    line to dispatch. Without it every line gets a refusal and *slash* is never
    called.
    """
    commands = slash_commands(intent)
    if not commands:
        return []
    try:
        allowed = bool(authorized())
    except (KeyError, TypeError, AttributeError):
        allowed = False
    if not allowed:
        return [DEVELOPER_REQUIRED % command.split()[0] for command in commands]
    return [slash(command) for command in commands]
