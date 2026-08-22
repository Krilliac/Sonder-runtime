"""Compatibility loader for the pre-package compaction application service."""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / "compaction.py"
_spec = spec_from_file_location("sonder_runtime.application._compaction_legacy", _path)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load {_path}")
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)
for _name in getattr(_module, "__all__", ()):
    globals()[_name] = getattr(_module, _name)

__all__ = list(getattr(_module, "__all__", ()))
