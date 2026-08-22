# WP1 Weather and Approximate-Location Packaging — 2026-08-21

Moved the canonical weather and approximate-location provider algorithms out
of the public `web_tools` surface and into:

- `sonder_runtime/adapters/weather.py`
- `sonder_runtime/adapters/location.py`

The root module retains identity-compatible aliases for legacy callers. The
packaged adapters continue to use the root's pinned `_request`/`_json_request`
transport seam, preserving URL validation, pinned connections, response
bounds, and existing test injection points. Weather retains Open-Meteo
geocoding fallback/ranking, coordinate and forecast-day bounds, unit selection,
and formatting. Location retains explicit consent at the typed facade,
response minimization, timezone normalization, place validation, and raw-IP
redaction.

## Evidence

- `python -m pytest -q tests/test_web_tools.py tests/test_weather_adapter.py tests/test_location_adapter.py tests/test_web_tools_security.py tests/test_weather_location_compatibility.py` — **59 passed**.
- `python -m compileall -q sonder_runtime/adapters/weather.py sonder_runtime/adapters/location.py web_tools.py` — pass.
- `python scripts/check_architecture.py` — pass.
- `tests/test_weather_location_compatibility.py` ratchets root identity, canonical source ownership, transport/URL seams, bounds, consent, and privacy behavior.

## Limitations

This is an `implemented_unverified` migration slice. The full repository
regression suite, server end-to-end production composition, and formal master
checklist promotion remain outside this focused evidence.
