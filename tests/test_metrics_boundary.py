"""WP1 Ninety-Ninth Slice: packaged metrics ownership regression."""

from pathlib import Path

from sonder_runtime.adapters.web import lifecycle
from sonder_runtime.platform import metrics as platform_metrics


def test_packaged_module_is_the_only_metrics_implementation():
    assert Path(platform_metrics.__file__).name == "metrics.py"
    assert platform_metrics.MetricsRegistry.__module__ == platform_metrics.__name__
    assert platform_metrics.default_registry.__module__ == platform_metrics.__name__


def test_root_metrics_module_is_retired():
    assert not (Path(__file__).resolve().parents[1] / "sonder_metrics.py").exists()


def test_lifecycle_uses_canonical_metrics_boundary():
    assert lifecycle.MetricsRegistry is platform_metrics.MetricsRegistry


def test_typed_lifecycle_settings_override_environment_without_eager_construction(
    monkeypatch,
):
    from sonder_runtime.platform.config import (
        CapacityConfig,
        ObservabilityConfig,
        SonderConfig,
    )

    lifecycle.reset_for_tests()
    lifecycle.configure(
        SonderConfig(
            capacity=CapacityConfig(http_requests=6, queue_depth=11),
            observability=ObservabilityConfig(metrics_enabled=False),
        )
    )
    monkeypatch.setenv("SONDER_MAX_CONCURRENT_REQUESTS", "99")
    monkeypatch.setenv("SONDER_QUEUE_DEPTH", "99")
    monkeypatch.setenv("SONDER_METRICS", "1")

    assert lifecycle._instance is None
    instance = lifecycle.get()
    assert instance._max_concurrent == 6
    assert instance._queue_depth == 11
    assert instance.metrics.enabled is False
    lifecycle.reset_for_tests()


def test_metric_names_and_labels_remain_unchanged():
    registry = platform_metrics.MetricsRegistry(enabled=False)

    assert registry.requests_total is not None
    assert registry.request_duration_seconds is not None
    assert registry.active_requests is not None
    assert registry.auth_failures_total is not None


def test_enabled_metric_names_and_label_sets_remain_unchanged():
    if not platform_metrics.PROMETHEUS_AVAILABLE:
        return
    registry = platform_metrics.MetricsRegistry()
    collectors = registry._registry._names_to_collectors
    expected = {
        "sonder_build": ("version", "commit"),
        "sonder_process": (),
        "sonder_requests": ("route", "result"),
        "sonder_request_duration_seconds": ("route",),
        "sonder_active_requests": (),
        "sonder_request_cache": ("result",),
        "sonder_model_calls": ("tier", "result"),
        "sonder_model_call_duration_seconds": ("tier",),
        "sonder_model_backend_phase_duration_seconds": ("backend", "phase"),
        "sonder_model_token_throughput_per_second": ("backend", "direction"),
        "sonder_model_load_states": ("backend", "state"),
        "sonder_sqlite_lock_wait_seconds": ("store",),
        "sonder_task_states": ("kind", "state"),
        "sonder_autopilot_runs": ("result",),
        "sonder_backup_age_seconds": (),
        "sonder_backup_runs": ("result",),
        "sonder_disk_free_bytes": ("path_class",),
        "sonder_redaction_failures": (),
        "sonder_auth_failures": ("reason",),
    }
    for name, labels in expected.items():
        collector = collectors[name]
        assert tuple(collector._labelnames) == labels
