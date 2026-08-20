"""Compatibility import for the canonical packaged metrics implementation."""

from sonder_runtime.platform import metrics as _metrics

CONTENT_TYPE_LATEST = _metrics.CONTENT_TYPE_LATEST
MetricsRegistry = _metrics.MetricsRegistry
PROMETHEUS_AVAILABLE = _metrics.PROMETHEUS_AVAILABLE
default_registry = _metrics.default_registry

if PROMETHEUS_AVAILABLE:
    # Preserve the optional public symbols that the historical root module
    # exposed when prometheus_client was installed.
    CollectorRegistry = _metrics.CollectorRegistry
    Counter = _metrics.Counter
    Gauge = _metrics.Gauge
    Histogram = _metrics.Histogram
    generate_latest = _metrics.generate_latest
