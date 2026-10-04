"""Typed "refused before mutating anything" signal for legacy selfmod stages.

A journaled selfmod stage admits its effect-journal intent before the legacy
stage body runs.  An exception from that body normally leaves the intent
``uncertain``: the stage may have replaced live bytes before it failed, so
only trusted reconciliation can settle it, and the run is fenced meanwhile.

Some legacy refusals are provably benign: ``selfmod.deploy`` and
``selfmod.rollback`` check their preconditions (phase, the process-safe
deployment lock, the source tree still matching the proposal, the
tested-bytes record, deployed bytes unchanged since deployment) before they
write anything.  The legacy module raises :class:`SelfmodStageNotApplied`
for exactly those refusals, and the stage journal settles the intent as a
``failed`` outcome with a ``:not-applied`` receipt instead of leaving it
uncertain -- so fixing the cause (waiting for the lock, reverting a manual
edit) and retrying works, while a failure after the first mutation still
fences the run.
"""
from __future__ import annotations

from collections.abc import Sequence


class SelfmodStageNotApplied(RuntimeError):
    """A legacy selfmod stage refused before it mutated source or run state.

    Raise it only where nothing the stage exists to change has changed yet
    (ledger audit events and the released deployment lock are bookkeeping,
    not the stage's effect).  It subclasses ``RuntimeError`` so callers that
    already handle the legacy refusals keep working unchanged.
    """


def require_atomic_checkout_promotion(changed_files: Sequence[str]) -> None:
    """Refuse a checkout-wide sequence before any live file is replaced.

    One file's replacement is atomic. Coupled changes need an immutable
    staged release and one release-pointer switch, not sequential copies.
    """
    if len(changed_files) != 1:
        raise SelfmodStageNotApplied(
            "checkout selfmod deployment supports exactly one changed file; "
            "use staged managed release activation for multi-file changes"
        )


__all__ = ["SelfmodStageNotApplied", "require_atomic_checkout_promotion"]
