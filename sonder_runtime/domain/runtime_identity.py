"""Pure rendering of the model identity facts included in a request prompt."""

from __future__ import annotations


def runtime_identity_block(model: str, cloud: bool = False) -> str:
    """Render authoritative facts about the model serving one request.

    The caller has already resolved ``model``.  This function deliberately has
    no access to tier tables or process state: a missing model must produce no
    claim rather than a guessed identity.
    """
    try:
        current = str(model or "")
    except Exception:
        return ""
    if not current:
        return ""
    where = (
        "served by Ollama's hosted service, not on this machine"
        if cloud else
        "an open-weights model served by Ollama on this machine"
    )
    return (
        "Facts about what is serving this request (authoritative -- use these, "
        "never your own recollection):\n"
        "- The model answering right now is `%s`, %s. You are NOT ChatGPT, "
        "GPT-4, Claude, or Gemini, and you share no architecture or training "
        "run with them.\n"
        "- Sonder is the runtime around you (memory, tools, policy, grounding). "
        "Sonder is not a model and has no parameters of its own.\n"
        "- If asked about your architecture, parameter count, training data, "
        "training cutoff, or generation speed, and the answer is not in this "
        "block or in the conversation, say you do not know and point the caller "
        "at `ollama ps` or Sonder's diagnostics. Do NOT guess a number, and do "
        "not infer one from the model's name: a confident wrong figure is worse "
        "than an admission." % (current, where)
    )
