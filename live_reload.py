"""Compatibility alias for the packaged live-reload adapter."""

import sys as _sys

from sonder_runtime.adapters.web import live_reload as _live_reload

_sys.modules[__name__] = _live_reload
