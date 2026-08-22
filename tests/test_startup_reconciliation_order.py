from sonder_runtime.adapters.web import lifecycle


def test_default_startup_reconciliation_orders_jobs_autopilot_and_fleet(monkeypatch):
    calls = []

    class Jobs:
        def reconcile(self, *, now):
            calls.append(("jobs", now))
            return 1

    class Autopilot:
        def reconcile_stale_runs(self, now):
            calls.append(("autopilot", now))
            return 2

    monkeypatch.setattr(lifecycle, "time", lifecycle.time)
    monkeypatch.setattr(
        "sonder_runtime.adapters.persistence.sqlite.job_registry.SQLiteDurableJobRegistry",
        lambda _path: Jobs(),
    )
    monkeypatch.setattr(
        "sonder_runtime.adapters.persistence.autopilot_repository.AutopilotRepository",
        lambda: Autopilot(),
    )
    monkeypatch.setattr(
        "sonder_runtime.adapters.persistence.fleet_store.reconcile_stale_owners",
        lambda *, now: calls.append(("fleet", now)) or {"interrupted": 3},
    )
    monkeypatch.setattr(lifecycle.time, "time", lambda: 1_700_000_000.0)

    assert lifecycle.RuntimeLifecycle._reconcile_startup_records() == 6
    assert [name for name, _ in calls] == ["jobs", "autopilot", "fleet"]
    assert calls[1][1] == calls[2][1] == 1_700_000_000.0
