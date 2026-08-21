# WP1 Two-Hundred-Ninety-Second Slice — consent-gated native web search

Packaged the legacy public web search behind the typed executor and native MCP
catalog. Search requires explicit operation-context cloud consent, clamps the
result count, preserves provider formatting, and excludes credentials and
transport controls from the native schema.

Focused adapter and catalog tests pass: **2 web-search tests** plus the native
MCP regression suite. The native catalog now reports **41** names against the
legacy source audit's 204 tools.
