from sonder_runtime.adapters import local_observability
from sonder_runtime.adapters.observability.latency_formatting import percentile


def test_percentile_is_owned_by_observability_package_and_preserves_alias():
    assert local_observability._percentile is percentile
    assert percentile([], 0.95) == 0
    assert percentile([30, 10, 20], 0.50) == 20
    assert percentile([30, 10, 20], 0.95) == 30


def test_percentile_does_not_mutate_samples():
    samples = [30, 10, 20]

    assert percentile(samples, 0.50) == 20
    assert samples == [30, 10, 20]
