"""Pure retrieval policies used by generation composition boundaries."""

from __future__ import annotations


def no_retrieve(conn=None, task=None):
    """Return no retrieved lessons for a deliberately clean generation.

    The retrieval hook accepts the connection and task supplied by the
    memory layer, but the teacher/clean route intentionally ignores both so
    its output is not augmented with local lessons before grounding and
    distillation.
    """
    del conn, task
    return []
