"""Bounded inventory and selection helpers for local inference backends.

The runtime must distinguish an installed backend from a backend that is
healthy, configured, or suitable for a particular model.  This module only
performs cheap local presence checks and a pure selection pass; it never
starts a provider, contacts a network endpoint, or treats an executable as
proof that CUDA or model residency works.
"""

from __future__ import annotations

import importlib.util
import shutil
from functools import lru_cache
from collections.abc import Callable, Mapping


_BACKEND_PROBES = {
    "ollama": {"executables": ("ollama",), "modules": ()},
    "llamacpp": {
        "executables": ("llama-server", "llama-cli"),
        "modules": ("llama_cpp",),
    },
    "vllm": {"executables": ("vllm",), "modules": ("vllm",)},
    "tensorrt-llm": {
        "executables": ("trtllm-serve",),
        "modules": ("tensorrt_llm",),
    },
}


@lru_cache(maxsize=1)
def probe_cuda_runtime() -> bool:
    """Return true only when an installed runtime reports usable CUDA.

    A display adapter or ``nvidia-smi`` entry is not enough evidence for model
    execution. PyTorch is optional; when it is absent or cannot initialize,
    CUDA remains unconfirmed and callers retain their CPU/non-CUDA fallback.
    """
    try:
        if importlib.util.find_spec("torch") is None:
            return False
        import torch  # type: ignore

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _present(
    spec: Mapping[str, object],
    *,
    which: Callable[[str], str | None],
    find_spec: Callable[[str], object | None],
) -> tuple[str, ...]:
    evidence: list[str] = []
    for executable in spec.get("executables", ()):
        name = str(executable)
        try:
            found = which(name)
        except Exception:
            found = None
        if found:
            evidence.append("executable:%s" % name)
    for module in spec.get("modules", ()):
        name = str(module)
        try:
            found = find_spec(name)
        except Exception:
            found = None
        if found is not None:
            evidence.append("module:%s" % name)
    return tuple(evidence)


def probe_backend_inventory(
    *,
    which: Callable[[str], str | None] | None = None,
    find_spec: Callable[[str], object | None] | None = None,
) -> dict[str, dict[str, object]]:
    """Return bounded, path-free presence facts for local backends.

    The returned ``installed`` flag means only that a known executable or
    Python package was found.  ``ready`` is deliberately absent: endpoint
    health, model compatibility, permissions, and accelerator support require
    the owning provider adapter's live checks.
    """
    which = which or shutil.which
    find_spec = find_spec or importlib.util.find_spec
    result: dict[str, dict[str, object]] = {}
    for backend, spec in _BACKEND_PROBES.items():
        evidence = _present(spec, which=which, find_spec=find_spec)
        result[backend] = {
            "installed": bool(evidence),
            "evidence": evidence,
            "readiness": "not-probed",
        }
    return result


def select_backend(
    inventory: Mapping[str, Mapping[str, object]],
    *,
    cuda_available: bool = False,
    model_format: str = "",
) -> dict[str, object]:
    """Choose an installed local backend without overclaiming acceleration.

    ``model_format`` is a hint such as ``gguf``, ``awq``, or ``gptq``.  The
    result is advisory metadata for diagnostics and scheduling; callers still
    need a provider health check before dispatch.
    """
    fmt = str(model_format or "").strip().lower().replace("-", "_")
    installed = {
        name for name, row in inventory.items()
        if isinstance(row, Mapping) and row.get("installed") is True
    }
    if cuda_available and fmt == "gguf" and "llamacpp" in installed:
        selected, reason = "llamacpp", "CUDA and a GGUF-capable llama.cpp install are present"
    elif cuda_available and fmt in {"awq", "gptq"} and "tensorrt-llm" in installed:
        selected, reason = "tensorrt-llm", "CUDA and a GPTQ/AWQ TensorRT-LLM install are present"
    elif cuda_available and fmt in {"awq", "gptq"} and "vllm" in installed:
        selected, reason = "vllm", "CUDA and a GPTQ/AWQ vLLM install are present"
    elif "ollama" in installed:
        selected, reason = "ollama", "Ollama is installed; provider readiness remains unprobed"
    elif "llamacpp" in installed:
        selected, reason = "llamacpp", "llama.cpp is installed; provider readiness remains unprobed"
    elif "vllm" in installed:
        selected, reason = "vllm", "vLLM is installed; provider readiness remains unprobed"
    else:
        selected, reason = "cpu", "no supported local provider presence was detected"
    return {
        "backend": selected,
        "reason": reason,
        "accelerated": selected not in {"cpu", "ollama"} and cuda_available,
        "candidates": tuple(sorted(installed)) + (("cpu",) if "cpu" not in installed else ()),
    }


__all__ = ["probe_backend_inventory", "probe_cuda_runtime", "select_backend"]
