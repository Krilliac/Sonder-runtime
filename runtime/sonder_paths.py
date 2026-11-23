"""Compatibility alias for the canonical packaged path implementation.

New code should import :mod:`sonder_runtime.platform.paths`. This module is
kept for release tooling and legacy installations; the module alias preserves
the historical import identity and monkeypatch surface.
"""
from sonder_runtime.platform import paths as _paths
import sys as _sys

_sys.modules[__name__] = _paths
