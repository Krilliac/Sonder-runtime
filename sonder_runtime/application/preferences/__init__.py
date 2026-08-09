"""Typed preference-management application boundary."""

from .use_cases import PreferenceService, render_preference_result

__all__ = ["PreferenceService", "render_preference_result"]
