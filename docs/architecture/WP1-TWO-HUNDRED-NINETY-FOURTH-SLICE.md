# WP1 Two-Hundred-Ninety-Fourth Slice — privacy-gated location lookup

Packaged approximate location lookup behind the typed executor and native MCP
catalog. It requires both explicit location consent and cloud consent, and
durable evidence contains only a coarse label; raw IP data is never retained
or emitted by the adapter.

Focused location and native catalog tests pass: **2 location tests** plus the
native MCP regression suite. The native catalog now reports **43** names.
