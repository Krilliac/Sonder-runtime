"""Canonical preference-text codec adapter.

This adapter owns the narrow ``PreferenceCodec`` port boundary while keeping
the legacy preference module lazy and injectable for tests and compatibility.
"""
from __future__ import annotations

import importlib

from ..application.ports.preferences import PreferenceModuleProvider


def _default_preference_module():
    return importlib.import_module("preference_learning")


class PreferenceCodecAdapter:
    """Adapt the legacy preference-learning module to the application port."""

    def __init__(
        self, module_provider: PreferenceModuleProvider | None = None
    ) -> None:
        self._module_provider = module_provider or _default_preference_module

    def extract(self, text):
        return self._module_provider().extract_preferences(text)

    def normalize(self, text):
        return self._module_provider().normalize_preference(text)

    def key(self, text):
        return self._module_provider().preference_key(text)

    def is_stable(self, text, source_text=None):
        return self._module_provider().is_stable_preference(text, source_text)

    def format(self, rows):
        return self._module_provider().format_preferences(rows)
