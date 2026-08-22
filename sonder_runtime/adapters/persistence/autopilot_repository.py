"""Application-facing adapter for the durable autopilot run ledger.

The persistence module owns SQLite details; this adapter owns the
``AutomationRepository`` port. Keeping it beside its database owner removes
one capability from the generic strangler boundary while the root
``autopilot_store`` name remains available only to immutable migrations.
"""
from __future__ import annotations

from . import autopilot_store


class AutopilotRepository:
    """Implement the automation port over the packaged autopilot store."""

    def create_run(self, objective: str, *, project: str = "", request_owner: str = "",
                   tier: str = "code", policy: str = "workspace", allow_web: bool = True,
                   max_failures: int = 3, max_tasks: int = 12, max_replans: int = 2,
                   adaptive: bool = True) -> dict:
        return autopilot_store.create_run(
            objective, project=project, request_owner=request_owner, tier=tier,
            policy=policy, allow_web=allow_web, max_failures=max_failures,
            max_tasks=max_tasks, max_replans=max_replans, adaptive=adaptive,
        )

    def get_run(self, selector: str = "", request_owner: str | None = None) -> dict | None:
        return autopilot_store.get_run(selector, request_owner=request_owner)

    def list_runs(self, include_finished: bool = True, limit: int = 20,
                  request_owner: str | None = None) -> list:
        return autopilot_store.list_runs(
            include_finished=include_finished, limit=limit, request_owner=request_owner
        )

    def claim_run(self, selector: str, owner_id: str, *, owner_pid: int,
                  request_owner: str | None = None,
                  lease_seconds: int | None = None) -> dict | None:
        kwargs = {"owner_pid": owner_pid, "request_owner": request_owner}
        if lease_seconds is not None:
            kwargs["lease_seconds"] = lease_seconds
        return autopilot_store.claim_run(selector, owner_id, **kwargs)

    def save_progress(self, run_id: str, owner_id: str, **changes) -> dict | None:
        return autopilot_store.save_progress(run_id, owner_id, **changes)

    def heartbeat(self, run_id: str, owner_id: str, lease_seconds: int | None = None) -> bool:
        if lease_seconds is None:
            return autopilot_store.heartbeat(run_id, owner_id)
        return autopilot_store.heartbeat(run_id, owner_id, lease_seconds)

    def request_pause(self, selector: str, request_owner: str | None = None) -> dict | None:
        return autopilot_store.request_pause(selector, request_owner=request_owner)

    def request_cancel(self, selector: str, request_owner: str | None = None) -> dict | None:
        return autopilot_store.request_cancel(selector, request_owner=request_owner)

    def control_flags(self, run_id: str, owner_id: str) -> dict:
        return autopilot_store.control_flags(run_id, owner_id)

    def finish_run(self, run_id: str, owner_id: str, status: str, *, summary: str = "",
                   final_report: str = "", last_error: str = "") -> dict | None:
        return autopilot_store.finish_run(
            run_id, owner_id, status, summary=summary,
            final_report=final_report, last_error=last_error,
        )

    def reconcile_stale_runs(self, now: float | None = None) -> int:
        return autopilot_store.reconcile_stale_runs(now)

    def events(self, selector: str = "", limit: int = 20,
               request_owner: str | None = None) -> list:
        return autopilot_store.events(selector, limit=limit, request_owner=request_owner)

    def snapshot(self, include_finished: bool = True, limit: int = 20,
                 request_owner: str | None = None) -> dict:
        return autopilot_store.snapshot(
            include_finished, limit=limit, request_owner=request_owner
        )
