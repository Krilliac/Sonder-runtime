# SELFMOD-001 reproducible evidence contract

Date: 2026-08-21

Added `sonder_runtime/application/selfmod/reproducer_contract.py`, a typed,
side-effect-free contract for concrete failure reproductions and benchmark
baselines. It bounds command arguments, requires an expected outcome,
acceptance criteria, and a SHA-256 artifact digest, and rejects invalid or
missing evidence.

Verification: `python -m pytest -q tests/test_selfmod_governance_reproducer.py --basetemp .pytest-selfmod-reproducer` — **4 passed**.

This contract is not yet wired into the legacy selfmod deployment lifecycle;
SELFMOD-001 therefore remains formally unverified.
