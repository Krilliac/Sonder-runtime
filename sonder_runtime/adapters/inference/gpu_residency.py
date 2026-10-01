"""Local GPU residency: keep the primary chat model loaded; spot contention.

Two operator concerns on a single consumer GPU (16 GB class):

* **Reloads between turns (G9).**  ``SONDER_KEEP_ALIVE`` (default ``2m``)
  unloads an idle Ollama model, and reloading a ~27B model costs ~20 s at
  the start of the next turn.  With ``SONDER_KEEP_PRIMARY_RESIDENT=1`` the
  primary chat model (the model the default chat route resolves to) is
  requested with ``keep_alive: -1`` so Ollama keeps it loaded; every other
  model keeps ``SONDER_KEEP_ALIVE``.  Off by default: pinning is only safe
  when that model is the only local model sharing the GPU, which
  :func:`gpu_sharing_findings` (the ``sonder_inference_gpu`` doctor check)
  reports on.  A pinned model stays loaded until Ollama restarts or
  ``ollama stop <model>`` -- including after the primary tier changes.
* **Silent spill (G10).**  A 15+ GB ``llama-server`` behind a local Sonder
  Inference leaves no room on a 16 GB card: any other local model that
  Ollama loads onto the GPU (a tier bound to Ollama, or the embedder) pushes
  the server's weights or KV into shared system memory, and decode drops by
  2-15x with no error.  :func:`gpu_sharing_findings` names each contender;
  nothing changes a default silently.
"""
from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable, Mapping

from ...domain import ollama_policy
from ...domain.model_routing import is_cloud_model_name
from ...domain.runtime_model_configuration import RuntimeModelConfiguration
from ..provider_bindings import PROVIDER_TIERS

ENV_KEEP_PRIMARY_RESIDENT = "SONDER_KEEP_PRIMARY_RESIDENT"
ENV_EMBED_ON_CPU = "SONDER_EMBED_ON_CPU"
ENV_EMBED_BASE_URL = "SONDER_EMBED_BASE_URL"
# Ollama's own switch that restricts its GPU discovery to one backend library.
# ``cpu`` makes the daemon find no GPU at all (0.34.4, 2026-09-30: `ollama ps`
# reports 100% CPU for every model and nvidia-smi does not move), so nothing
# it loads can share the card with a local Sonder Inference server.
ENV_OLLAMA_LLM_LIBRARY = "OLLAMA_LLM_LIBRARY"
RESIDENT_KEEP_ALIVE = -1  # a JSON number: Ollama reads a negative value as "forever"
_ON = frozenset({"1", "true", "yes", "on"})


def _enabled(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "") or "").strip().lower() in _ON


def local_ollama_pinned_to_cpu(env: Mapping[str, str] | None = None) -> bool:
    """Whether OLLAMA_LLM_LIBRARY=cpu keeps the local daemon off every GPU.

    Read from this process's environment, which on a workstation is the same
    User environment the daemon inherits; a daemon started before the value
    was set still has to be restarted, which the doctor detail says.
    """
    source = os.environ if env is None else env
    return str(source.get(ENV_OLLAMA_LLM_LIBRARY, "") or "").strip().lower() == "cpu"


def _embedder_loads_locally(env: Mapping[str, str]) -> bool:
    """False when SONDER_EMBED_BASE_URL sends embeddings to another host.

    A dedicated endpoint on a loopback address is still this machine's GPU.
    The opt-in local fallback forces ``num_gpu: 0``, so it never counts.
    """
    raw = str(env.get(ENV_EMBED_BASE_URL, "") or "").strip()
    if not raw:
        return True
    try:
        host = urllib.parse.urlparse(ollama_policy.normalize(raw)).hostname
    except ValueError:
        return True
    if not host:  # an unparsable origin is not provably remote
        return True
    return ollama_policy.is_loopback(raw)


def keep_primary_resident(env: Mapping[str, str] | None = None) -> bool:
    return _enabled(os.environ if env is None else env, ENV_KEEP_PRIMARY_RESIDENT)


def keep_alive_for(
    model: object, default: object, *, primary: Callable[[], object],
    env: Mapping[str, str] | None = None,
) -> object:
    """``keep_alive`` for one Ollama request naming ``model``.

    ``default`` unless the flag is on and ``model`` is the primary chat model
    (``primary()`` is only called when the flag is on; any failure keeps the
    default).
    """
    if not keep_primary_resident(env) or not isinstance(model, str) or not model:
        return default
    try:
        resolved = primary()
    except Exception:  # noqa: BLE001 - residency is an optimisation, never a failure
        return default
    return RESIDENT_KEEP_ALIVE if resolved == model else default


def _local_ollama(env: Mapping[str, str]) -> bool:
    try:
        return ollama_policy.is_loopback(str(env.get("OLLAMA_HOST", "") or "") or None)
    except Exception:  # noqa: BLE001 - an unparsable host is not provably local
        return False


def local_ollama_tier_models(env: Mapping[str, str], bindings) -> dict[str, str]:
    """``{tier: model}`` for tiers a local Ollama serves on this machine."""
    if not _local_ollama(env):
        return {}
    tiers = dict(RuntimeModelConfiguration.from_environment(env).tier_map())
    found: dict[str, str] = {}
    for tier in PROVIDER_TIERS:
        model = str(tiers.get(tier, "") or "").strip()
        if (bindings.tier_providers.get(tier) == "ollama" and model
                and not is_cloud_model_name(model)):
            found[tier] = model
    return found


def gpu_sharing_findings(env: Mapping[str, str], bindings, *, inference_local: bool) -> list[str]:
    """Plain statements of what would share the GPU with a local Inference.

    Nothing local Ollama loads can share the card while it is pinned to the
    CPU library (``OLLAMA_LLM_LIBRARY=cpu``); see :func:`gpu_sharing_report`
    for the statement the doctor prints in that case.
    """
    if local_ollama_pinned_to_cpu(env):
        return []
    return _contenders(env, bindings, inference_local=inference_local)


def gpu_sharing_report(env: Mapping[str, str], bindings, *, inference_local: bool) -> dict:
    """``{"findings": [...], "note": str}`` for the doctor.

    ``findings`` is what :func:`gpu_sharing_findings` returns. ``note`` is a
    single statement when OLLAMA_LLM_LIBRARY=cpu suppressed contenders: it
    names them, so a daemon that was never restarted after the variable was
    set is still visible to the operator.
    """
    contenders = _contenders(env, bindings, inference_local=inference_local)
    if not local_ollama_pinned_to_cpu(env):
        return {"findings": contenders, "note": ""}
    note = "local Ollama is pinned to the CPU library (%s=cpu)" % ENV_OLLAMA_LLM_LIBRARY
    if contenders:
        note += (
            ", so these cannot take the GPU (restart the daemon if it predates "
            "the setting): %s" % "; ".join(contenders)
        )
    return {"findings": [], "note": note}


def _contenders(env: Mapping[str, str], bindings, *, inference_local: bool) -> list[str]:
    findings: list[str] = []
    inference_tiers = sorted(
        tier for tier in PROVIDER_TIERS
        if bindings.tier_providers.get(tier) == "sonder_inference"
    )
    ollama_models = local_ollama_tier_models(env, bindings)
    if inference_tiers and inference_local:
        if ollama_models:
            findings.append(
                "tier(s) %s run local Ollama model(s) %s that load on the GPU "
                "beside the Sonder Inference server (tiers %s)"
                % (", ".join(sorted(ollama_models)),
                   ", ".join(sorted(set(ollama_models.values()))),
                   ", ".join(inference_tiers))
            )
        if (bindings.embedding_provider == "ollama" and _local_ollama(env)
                and _embedder_loads_locally(env)
                and not _enabled(env, ENV_EMBED_ON_CPU)):
            findings.append(
                "the Ollama embedder loads on the GPU beside the Sonder "
                "Inference server; set %s=1 to keep it on the CPU" % ENV_EMBED_ON_CPU
            )
    if keep_primary_resident(env) and len(set(ollama_models.values())) > 1:
        findings.append(
            "%s=1 pins the primary chat model while other local Ollama models "
            "(%s) can also load; they will contend with it"
            % (ENV_KEEP_PRIMARY_RESIDENT, ", ".join(sorted(set(ollama_models.values()))))
        )
    return findings


__all__ = [
    "ENV_EMBED_BASE_URL",
    "ENV_EMBED_ON_CPU",
    "ENV_KEEP_PRIMARY_RESIDENT",
    "ENV_OLLAMA_LLM_LIBRARY",
    "RESIDENT_KEEP_ALIVE",
    "gpu_sharing_findings",
    "gpu_sharing_report",
    "keep_alive_for",
    "keep_primary_resident",
    "local_ollama_pinned_to_cpu",
    "local_ollama_tier_models",
]
