# WP4 CTX-008 — Hardware-aware native context sizing

## Boundary

`sonder_runtime.domain.context.hardware_sizing` is a pure policy for choosing
the native context size for one model/KV-cache configuration. It receives a
measured successful context point and the current available VRAM (or RAM for
CPU execution). The caller owns hardware and provider probing, and must only
reuse a measurement for the same model, quantization, and KV-cache type.

The policy scales the measured context by current-versus-measured free memory,
then applies a 0.80 memory margin and a 0.90 token margin. It rounds down and
clamps to explicit minimum/maximum bounds. Missing or invalid measurements use
the deterministic 8,192-token fallback, which is also bounded by those limits.
The returned immutable `ContextSizing` records the source, reason, raw result,
and margins for explainability.

This slice does not modify hardware probes, `platform.context_policy`,
`platform.context_selection`, the context planner, compaction, or overflow
recovery. It has no provider, environment, filesystem, or network dependency.

## Measured inputs

`MeasuredContextCapability` contains the successful context token count, free
memory observed at that measurement, current free memory, and optional model
and KV-cache identifiers. The identifiers are provenance supplied by the
caller; the policy deliberately does not infer or validate them.

## Verification

```text
python -m pytest -q tests/test_wp4_ctx008.py
python -m compileall -q sonder_runtime/domain/context/hardware_sizing.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
git diff --check
```
