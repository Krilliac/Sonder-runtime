from sonder_runtime.application.control_plane import (
    CONTROL_PLANE_SECTIONS,
    ControlPlaneProviderError,
    ControlPlaneSnapshotService,
)
from sonder_runtime.interfaces.http.facades.control_plane import ControlPlaneFacade


def providers():
    return {name: (lambda name=name: ({"name": name},)) for name in CONTROL_PLANE_SECTIONS}


def test_control_plane_route_is_authenticated_and_renders_snapshot():
    facade = ControlPlaneFacade()
    route = facade.route("/v1/admin/control-plane?format=json")
    assert route is not None
    assert route.requires_auth is True
    status, payload = route.render(
        ControlPlaneSnapshotService(providers()), captured_at="now", revision=2
    )
    assert status == 200
    assert payload["snapshot"]["revision"] == 2
    assert len(payload["digest"]) == 64


def test_control_plane_route_returns_safe_unavailable_response():
    class Broken:
        def snapshot(self, **_kwargs):
            raise ControlPlaneProviderError("secret backend detail")

    route = ControlPlaneFacade().route("/v1/admin/control-plane")
    assert route is not None
    assert route.render(Broken(), captured_at="now") == (
        503, {"error": "control_plane_unavailable"}
    )


def test_unrelated_route_is_not_claimed():
    assert ControlPlaneFacade().route("/v1/admin/health") is None
