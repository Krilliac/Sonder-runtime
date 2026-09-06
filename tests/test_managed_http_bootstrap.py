from types import SimpleNamespace


def test_managed_http_binds_legacy_routes_after_owned_application(
    monkeypatch,
):
    """The contained child must not publish an unbound compatibility surface."""
    import sonder_runtime.bootstrap.legacy_interfaces as legacy
    import sonder_runtime.bootstrap.managed_http_runtime as managed

    events = []
    application = SimpleNamespace(
        memory=object(),
    )
    config = SimpleNamespace(
        capacity=SimpleNamespace(
            autopilot_runs=3,
            fleet_workers=4,
            training_jobs=5,
        ),
    )

    monkeypatch.setattr(
        legacy,
        "configure_legacy_interfaces",
        lambda: events.append(("interfaces",)),
    )
    monkeypatch.setattr(
        legacy,
        "configure_legacy_application",
        lambda value: events.append(("application", value)),
    )
    monkeypatch.setattr(
        legacy,
        "configure_legacy_capacity",
        lambda **values: events.append(("capacity", values)),
    )

    class Serve:
        def configure_thin_handlers(self, handlers):
            events.append(("thin", handlers))

    managed._configure_legacy_http_boundary(application, config, serve=Serve())

    assert [item[0] for item in events] == [
        "interfaces",
        "application",
        "capacity",
        "thin",
    ]
    assert events[1][1] is application
    assert events[2][1] == {
        "autopilot_runs": 3,
        "fleet_workers": 4,
        "training_jobs": 5,
    }
    handlers = events[3][1]
    assert set(handlers) == {"/v1/recall", "/v1/outcome"}
    assert handlers["/v1/recall"]._recall is application.memory
    assert handlers["/v1/outcome"]._outcome is application.memory
