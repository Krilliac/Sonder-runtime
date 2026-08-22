# WP1 Two-Hundred-Fiftieth Slice — memory-quality doctor boundary

## Boundary

Moved the read-only memory-quality doctor policy into
`sonder_runtime.bootstrap.doctor_checks.summarize_memory_quality`. The
packaged boundary now owns connection lifecycle, audit-failure degradation,
severity classification, hygiene classification, and diagnostic text.

`sonder_doctor._check_memory_quality` remains the compatibility entrypoint. It
still reads `SONDER_DB` and lazily imports the historical `memory_quality` and
memory-store collaborators, then injects those collaborators into the packaged
policy. This preserves existing imports, monkeypatch seams, and output while
keeping legacy-root knowledge out of the packaged boundary.

## Evidence

- `tests/test_bootstrap_doctor_checks.py` verifies clean, severe, hygiene,
  connection-failure, audit-failure, and connection-close behavior.
- `python -m pytest -q tests/test_bootstrap_doctor_checks.py tests/test_sonder_doctor.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_doctor.py`
- `git diff --check`

The unrelated WP1 Two-Hundred-Forty-Ninth launcher changes already present in
the shared worktree were preserved and are not part of this slice.
