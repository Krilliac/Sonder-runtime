"""Canonical platform resolution for local Ollama model roots."""
from __future__ import annotations

import os
from pathlib import Path


def model_roots(env: dict[str, str] | None = None) -> tuple[Path, ...]:
    """Return configured/default Ollama model roots without creating them."""
    values = os.environ if env is None else env
    configured = str(values.get("OLLAMA_MODELS", "")).strip()
    candidates = (
        [Path(configured).expanduser()]
        if configured
        else [Path.home() / ".ollama" / "models"]
    )
    return _unique_paths(candidates)


def _unique_paths(paths) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        candidate = Path(path).expanduser().resolve(strict=False)
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return tuple(result)


__all__ = ("model_roots",)
