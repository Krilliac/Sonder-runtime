"""Crash and profile digests: ports, the digest service and its presenters.

Tier 0 (pure readers, lanes A/B) always runs first; Tier 1 host debuggers and
profilers run as durable, permission-gated process jobs built from
host-owned argv templates. Interfaces render through ``presenters`` only.
"""
