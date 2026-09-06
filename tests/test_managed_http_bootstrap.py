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


def test_managed_http_keeps_owned_work_unavailable_when_control_is_disabled(
    monkeypatch,
):
    """The default foreground profile must not construct an app-work authority."""
    import sonder_runtime.bootstrap.managed_http_runtime as managed

    config = SimpleNamespace(app_control=SimpleNamespace(enabled=False))

    class Serve:
        _APP_CONTROL_BINDING = object()

    def unexpected(*args, **kwargs):
        raise AssertionError("disabled app control must not compose owned work")

    monkeypatch.setattr(
        "sonder_runtime.bootstrap.app_managed_work_http.install_owned_work_http",
        unexpected,
    )
    assert managed._install_owned_app_work_if_enabled(
        object(), config, serve=Serve()
    ) is None


def test_managed_http_composes_owned_work_with_exact_startup_identities(monkeypatch):
    """Enabled composition passes the owned Application and legacy runtime through."""
    import sonder_runtime.bootstrap.app_managed_work_http as work_http
    import sonder_runtime.bootstrap.legacy_root as legacy_root
    import sonder_runtime.bootstrap.managed_http_runtime as managed
    from sonder_runtime.adapters.security.permission_policy import PermissionPolicyProvider

    application = object()
    control = SimpleNamespace()
    legacy_runtime = object()
    config = SimpleNamespace(app_control=SimpleNamespace(enabled=True))
    calls = []
    binding = object()

    monkeypatch.setattr(legacy_root, "runtime", lambda: legacy_runtime)

    def install(control_value, *, application, runtime, permission_engine):
        calls.append((control_value, application, runtime, permission_engine))
        control_value._work_binding = binding
        return binding

    monkeypatch.setattr(work_http, "install_owned_work_http", install)
    serve = SimpleNamespace(_APP_CONTROL_BINDING=control)

    assert managed._install_owned_app_work_if_enabled(
        application, config, serve=serve
    ) is binding
    assert calls[0][0] is control
    assert calls[0][1] is application
    assert calls[0][2] is legacy_runtime
    assert type(calls[0][3]) is PermissionPolicyProvider


def test_managed_http_refuses_enabled_work_without_typed_control(monkeypatch):
    import pytest
    import sonder_runtime.bootstrap.managed_http_runtime as managed
    from sonder_runtime.application.ports.runtime_owner import OwnerRefused

    config = SimpleNamespace(app_control=SimpleNamespace(enabled=True))
    with pytest.raises(OwnerRefused, match="typed app-control binding"):
        managed._install_owned_app_work_if_enabled(
            object(), config, serve=SimpleNamespace(_APP_CONTROL_BINDING=None)
        )
