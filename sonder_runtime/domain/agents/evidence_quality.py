"""Pure evidence-quality checks for agent tool observations.

A tool observation only counts as evidence when its contract says so: the
codegen loop's host-rendered terminal verdict, a fetched page with readable
content, an archive listing that validated. The generic success predicate is
injected by the caller. Moved from ``server.py`` in the WP1 Three-Hundred-Sixth
Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import json


def codegen_build_succeeded(observation):
    """Read only the host-rendered terminal verdict from the codegen loop."""
    lines = {line.strip() for line in str(observation or "").splitlines()}
    return (
        "BUILD SUCCEEDED" in lines
        and not any(line.startswith("BUILD FAILED") for line in lines)
        and "BUILD DID NOT RUN" not in lines
        and not any(line.startswith("BUILD MEASUREMENT INCOMPLETE") for line in lines)
    )


def tool_observation_ok(tool_name, observation, *, observation_ok):
    """Apply evidence-quality checks that are specific to a tool contract.

    ``observation_ok(observation)`` is the generic success predicate, injected
    because its ``ERROR:`` prefix parse is recorded in the shrink-only
    error-signal baseline under its current scope.
    """
    if str(tool_name or "") == "ensemble_codegen_build_loop":
        return codegen_build_succeeded(observation)
    if str(tool_name or "") == "web_fetch" and observation is None:
        return False
    if not observation_ok(observation):
        return False
    if str(tool_name or "") == "archive_list":
        try:
            return bool(json.loads(str(observation or "")).get("valid"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    if str(tool_name or "") != "web_fetch":
        return True
    # A transport-level success with an empty page is not grounding. Require
    # at least one readable letter or digit before a fetch can satisfy the
    # research agent's required-tool evidence gate. Keep the generic success
    # predicate unchanged because empty/zero-ish output is valid for several
    # execution and inspection tools.
    text = str(observation or "").strip()
    return bool(text and any(character.isalnum() for character in text))
