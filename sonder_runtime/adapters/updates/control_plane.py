"""Read-only update status adapter for operator projections."""
from __future__ import annotations


def read_update_status() -> dict:
    """Read the bounded public update status without coupling bootstrap to engine."""
    # The engine is loaded only when this read is requested.  Keeping the
    # import inside the adapter avoids a bootstrap -> engine -> bootstrap cycle.
    from .engine import UpdateManager

    return UpdateManager().status()


__all__ = ["read_update_status"]
