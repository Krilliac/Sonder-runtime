# WP1 Two-Hundred-Ninety-Third Slice — consent-gated native weather

Packaged the legacy Open-Meteo weather lookup behind the typed executor and
native MCP catalog. The adapter requires explicit cloud consent, clamps the
forecast horizon to seven days, preserves the provider's unit validation and
formatting, and emits only location metadata as durable evidence.

Focused weather and native catalog tests pass: **2 weather tests** plus the
native MCP regression suite. The native catalog now reports **42** names.
