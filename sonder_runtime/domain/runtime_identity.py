"""Pure facts about the model identity included in a request prompt.

The wording of the identity block is an editable prompt
(``prompts/runtime_identity.md``, rendered by
``sonder_runtime.adapters.prompt_store.runtime_identity_block``). This module
keeps the part that must not be editable: deciding which facts are true for
one request.
"""

from __future__ import annotations


def runtime_identity_fields(
    model: str, cloud: bool = False, provider: str | None = None,
) -> dict[str, str] | None:
    """Template fields for the model serving one request, or ``None``.

    The caller has already resolved ``model``.  This function deliberately has
    no access to tier tables or process state: a missing model must produce no
    claim rather than a guessed identity, so ``None`` means "leave the block
    out".
    """
    try:
        current = str(model or "")
    except Exception:
        return None
    if not current:
        return None
    inference = not cloud and provider == "sonder_inference"
    where = (
        "served by Ollama's hosted service, not on this machine"
        if cloud else
        "an open-weights model served through Sonder Inference"
        if inference else
        "an open-weights model served by Ollama on this machine"
    )
    diagnostics = (
        "Sonder Inference's `/v1/sonder/health` or Sonder's diagnostics"
        if inference else "`ollama ps` or Sonder's diagnostics"
    )
    return {"model": current, "where": where, "diagnostics": diagnostics}
