# WP7-MODEL-001 — Measured model and hardware calibration

The routing calibration seam records observations for an exact model artifact,
quantization, and hardware identity. It exposes residency, throughput, p95
latency, sample count, timestamp, and a deterministic digest. The application
registry upserts newer observations, rejects stale observations at selection,
filters by measured residency, and chooses the highest measured throughput with
stable latency/residency/freshness/identity tie-breakers.

This slice is intentionally additive. It does not edit the formal master-spec
checkboxes, persist measurements, or claim that an uncalibrated model is safe:
the caller receives no selection when no fresh measured profile fits.

Validation: `tests/test_wp7_calibration.py` plus the architecture, requirement
evidence, compileall, and diff gates.
