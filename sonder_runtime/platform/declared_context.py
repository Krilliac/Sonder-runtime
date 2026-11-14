"""The context window a model declares for itself in its Ollama parameters.

Ollama reloads a model whenever ``num_ctx`` differs between requests, so a model
called by name keeps the window its own Modelfile declares instead of one
derived from probe success.  Pure functions only: no I/O, no globals.
"""

import os

_NUM_CTX = "num_ctx"
_PIN_VARIABLES = ("SONDER_CONTEXT_SIZE", "SONDER_SESSION_NUM_CTX")


def declared_num_ctx(parameters_text) -> int | None:
    """Return the positive ``num_ctx`` in an ``/api/show`` ``parameters`` text.

    The text is lines of ``name<whitespace>value``.  Several parameters may be
    present and a parameter may repeat; the last ``num_ctx`` line wins, as in a
    Modelfile.  Anything that is not a positive integer yields ``None``.
    """
    if not isinstance(parameters_text, str):
        return None
    declared = None
    for line in parameters_text.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or parts[0] != _NUM_CTX:
            continue
        token = parts[1].strip().strip('"')
        if token.isascii() and token.isdigit() and int(token) > 0:
            declared = int(token)
        else:
            declared = None
    return declared


def operator_pinned() -> bool:
    """True when the operator pinned a context size in the environment."""
    return any(str(os.environ.get(name) or "").strip() for name in _PIN_VARIABLES)


def resolve_declared_context(declared, advertised) -> int | None:
    """Clamp a declared window by the model's advertised ``context_length``.

    ``declared`` or ``advertised`` of ``None`` (or non-positive) mean unknown.
    No declaration yields ``None`` so the caller falls back to its own policy.
    """
    if not isinstance(declared, int) or isinstance(declared, bool) or declared <= 0:
        return None
    if isinstance(advertised, int) and not isinstance(advertised, bool) and 0 < advertised < declared:
        return advertised
    return declared
