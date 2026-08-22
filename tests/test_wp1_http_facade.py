"""Focused coverage for the root-free HTTP health/status facade."""

from dataclasses import dataclass

from sonder_runtime.interfaces.http.facades.health_status import HealthStatusFacade


@dataclass
class _Metrics:
    def render(self):
        return "sonder_requests_total 2\n"


class _Lifecycle:
    metrics = _Metrics()

    def live_payload(self):
        return {"status": "alive"}

    def ready_payload(self):
        return 503, {"ready": False, "reason": "probe pending"}

    def health_payload(self):
        return {"state": "READY", "dependencies": {}}

    def version_payload(self):
        return {"version": "test", "commit": "abc"}


def test_facade_classifies_only_bounded_lifecycle_family():
    facade = HealthStatusFacade()
    assert facade.route("/live").requires_auth is False
    assert facade.route("/health/").path == "/health"
    assert facade.route("/v1/chat/completions") is None


def test_facade_renders_injected_lifecycle_without_server_import():
    facade = HealthStatusFacade()
    lifecycle = _Lifecycle()

    assert facade.route("/live").render(lifecycle) == (200, {"status": "alive"})
    assert facade.route("/ready").render(lifecycle) == (
        503, {"ready": False, "reason": "probe pending"}
    )
    assert facade.route("/metrics").media_type.startswith("text/plain")
    assert facade.route("/metrics").render(lifecycle)[1].endswith("\n")


def test_facade_has_no_legacy_root_imports():
    source = open(
        "sonder_runtime/interfaces/http/facades/health_status.py",
        encoding="utf-8",
    ).read()
    assert "import server" not in source
    assert "importlib" not in source
