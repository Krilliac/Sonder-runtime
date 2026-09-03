"""Pure classification of whether a lane can reach a verifier at all.

A completion claim may rest on a passing verifier only where the lane's own
gates admit one; a lane that can call none must not be told it failed to.
The read-only allow-list is injected by the caller. Moved from ``server.py``
in the WP1 Three-Hundred-Sixth Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations


# Verifiers whose passing result is a citation a completion claim can rest on.
#
# Deliberately NOT members of _WORK_VALIDATION_TOOLS: _agent_validation_covers
# has no branch for these names and falls through to False, so adding them
# there would set validation_ok=False and stamp the run VALIDATION_FAILED --
# running the tests would be the thing that failed the run.
VERIFICATION_TOOLS = frozenset({
    "test_run", "build_run", "lint_run", "typecheck_run",
})


def verifier_reachable(read_only, allowed_tools, *, read_only_tools):
    """Whether any verifier could have been called in this lane at all.

    Read from the two gates the dispatcher actually enforces -- the read-only
    policy, which admits only ``REPOSITORY_READ_ONLY_TOOLS``, and the lane's
    own ``tool_allowlist`` -- rather than from a list of lane names, so a lane
    added later is classified by what it can do instead of by whether someone
    remembered to add it here.

    This exists because a demand nothing in the lane can satisfy is not a gate.
    Four allowlists in this file admit no member of
    ``VERIFICATION_TOOLS``: ``REPOSITORY_READ_ONLY_TOOLS`` (repository
    workers), ``_AUTOPILOT_OBSERVE_TOOLS``, the chat/web research allowlist,
    and the selfmod editor's. Leading their answers -- every weather question
    among them -- with "claimed completion without a passing verification
    (test_run, build_run, lint_run or typecheck_run)" names tools the lane is
    forbidden from calling, and no run in it could ever clear the line. A
    standing with no OFF state is a banner, and a banner teaches a reader to
    skip exactly where a real warning would appear.

    The measurement is unchanged either way, and still reaches the caller
    through the end-report standing line, which ships on every run.

    ``read_only_tools`` is the read-only policy's allow-list, injected so this
    classification never imports the dispatcher's tool tables.
    """
    reachable = set(VERIFICATION_TOOLS)
    if read_only:
        reachable &= set(read_only_tools)
    if allowed_tools is not None:
        reachable &= set(allowed_tools)
    return bool(reachable)
