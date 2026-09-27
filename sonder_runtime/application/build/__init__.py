"""C++ build model, build jobs and the bounded build-fix loop.

``BuildToolServices`` is the aggregate the typed tools, the HTTP and REPL
facades and the model-context brief reach. ``fix`` is ``None`` until the
fix loop is composed; every surface reports that instead of failing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model_service import BuildModelService
from .run_service import BuildJobService


@dataclass(frozen=True)
class BuildToolServices:
    model: BuildModelService
    jobs: BuildJobService
    fix: Any = None


__all__ = ["BuildToolServices"]
