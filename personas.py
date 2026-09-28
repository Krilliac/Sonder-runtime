"""personas — swappable system-prompt presets for Sonder Runtime.

Lets Sonder Runtime serve non-coders too: a plain-language explainer, a code
reviewer, a teacher — not just the default coder. The text of each persona is
an editable Markdown prompt (``prompts/personas/<name>.md``, overridable per
operator; see ``sonder_runtime.adapters.prompt_store``), read fresh each turn.
"""
from sonder_runtime.adapters import prompt_store as _prompts

DEFAULT = "coder"

_NAMES = tuple(sorted(
    name.split("/", 1)[1] for name in _prompts.CATALOG if name.startswith("personas/")
))


def get(name):
    """Return the system prompt for `name`, falling back to DEFAULT.

    Case/whitespace-insensitive; None or unknown names fall back to coder.
    """
    key = (name or DEFAULT).strip().lower()
    if key not in _NAMES:
        key = DEFAULT
    return _prompts.render("personas/" + key)


def names():
    """Return the sorted list of available persona names."""
    return list(_NAMES)


def __getattr__(attribute):
    # ``PERSONAS`` used to be a literal dict; keep the name for callers that
    # read it, but make it a fresh snapshot of the editable prompts.
    if attribute == "PERSONAS":
        return {name: get(name) for name in _NAMES}
    raise AttributeError("module %r has no attribute %r" % (__name__, attribute))
