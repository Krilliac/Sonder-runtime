"""Globally unique fanout worker identity for lease ownership.
"""
from __future__ import annotations

import uuid


FANOUT_WORKER_INSTANCE = uuid.uuid4().hex


def fanout_worker_id(instance_token, pid, thread_ident):
    """Return a globally unique durable-receipt lease owner identifier.

    A fanout database may be intentionally shared by several runtime hosts.
    PID/thread pairs are only process-local and can collide across hosts (or
    after a quick PID reuse), which would let two workers impersonate one
    lease owner.  The random instance token is created once per import/process
    and remains stable for its worker's lifetime while fencing every other
    runtime instance.
    """
    return "fanout-%s-%d-%d" % (instance_token, pid, thread_ident)
