"""WP1 Ninety-Ninth Slice: packaged metrics ownership regression."""

from pathlib import Path

import sonder_metrics

from sonder_runtime.adapters.web import lifecycle
from sonder_runtime.platform import metrics as platform_metrics


def test_platform_metrics_preserves_root_registry_identity():
    assert platform_metrics.MetricsRegistry is sonder_metrics.MetricsRegistry
    assert platform_metrics.default_registry is sonder_metrics.default_registry


def test_packaged_module_owns_implementation_and_root_is_only_compatibility():
    assert Path(platform_metrics.__file__).name == "metrics.py"
    assert Path(sonder_metrics.__file__).name == "sonder_metrics.py"
    assert platform_metrics.MetricsRegistry.__module__ == platform_metrics.__name__
    assert sonder_metrics.MetricsRegistry.__module__ == platform_metrics.__name__
    assert platform_metrics.default_registry.__module__ == platform_metrics.__name__
    if platform_metrics.PROMETHEUS_AVAILABLE:
        for name in ("CollectorRegistry", "Counter", "Gauge", "Histogram", "generate_latest"):
            assert getattr(sonder_metrics, name) is getattr(platform_metrics, name)


def test_root_and_packaged_imports_share_the_same_default_registry():
    assert sonder_metrics.default_registry() is platform_metrics.default_registry()


def test_lifecycle_uses_canonical_metrics_boundary():
    assert lifecycle.MetricsRegistry is platform_metrics.MetricsRegistry


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
