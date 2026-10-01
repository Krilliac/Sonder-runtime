"""Pure formatting policies for composing prompt sections.

Why ``server._SYSTEM_CONTEXT`` pins the disk-backed prompt parts for one turn
(moved here from ``server.py``, which is line-capped; the wording is the
original):

One turn can build the system prompt more than once, and each build re-read
system_profile.md, the emotion vectors and the goal store from disk.
Measured: a workbench-agent turn builds it twice (the agent loop, then the
negative-claim reviewer at finalization) and a routed work request builds it
three times (execution-mode router, then the agent, then that reviewer).
Every one of those prompts is sent to a model -- none is discarded -- so this
cannot be fixed by dropping a build. With an edit landing between two reads,
one turn told the router "never use the network" and, in the same turn, told
the agent "always use the network".

Per-REQUEST freshness is deliberate: system_profile.py exists so an operator
can edit standing instructions while the server runs. Per-TURN consistency is
what was missing, so the parts are read once per turn and reused, not cached
for the life of the process.

``_runtime_identity_block()`` is deliberately NOT pinned. It names the model
answering THIS call, and the two consumers in a routed turn can run on
different tiers; pinning it would make the second prompt state the first
one's model, which is the exact failure that block exists to prevent.

The owner playbook index is the fourth pinned part. Unlike the other three it
is frozen per SESSION (an explicit ``/playbooks reload`` refreshes it), so an
approval landing mid-session cannot move the stable prompt prefix and bust the
provider prefix cache.
"""

from __future__ import annotations


def join_system_parts(*parts) -> str:
    """Join non-empty prompt sections with one blank line between sections."""
    return "\n\n".join(part for part in parts if part)
