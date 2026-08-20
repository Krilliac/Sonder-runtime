# WP1 One-Hundred-Eleventh Slice — retire the metrics root delegate

The canonical metrics implementation has lived in
`sonder_runtime.platform.metrics` since the Ninety-Ninth Slice. This slice
removes the now-unneeded root-level `sonder_metrics.py` compatibility delegate
and moves the remaining regression imports to the packaged module.

Scope is limited to metrics ownership. It does not touch `unsafe_lab`, its
activation/audit state, or the architecture policy surrounding that module.

## Evidence

- A repository source audit found no production import of `sonder_metrics`.
- `sonder_metrics.py` is listed in `RETIRED_ROOT_MODULES`, and the architecture
  regression rejects reintroduction of the root file.
- Focused metrics, inference-telemetry, request-cache, compile, architecture,
  requirement-evidence, and diff gates are required for acceptance.

This advances ARCH-001 (one authoritative metrics implementation), ARCH-002
(root cleanup), and ARCH-003 (removal of the transitional delegate).
