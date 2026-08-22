import datetime

import server
from sonder_runtime.domain.retry_after import retry_after_seconds


def test_server_keeps_retry_after_compatibility_alias():
    assert server._retry_after_seconds is retry_after_seconds


def test_retry_after_policy_supports_dates_and_bounds_values():
    now = datetime.datetime(2026, 8, 11, 21, 0, tzinfo=datetime.timezone.utc)
    assert retry_after_seconds(
        {"Retry-After": "Tue, 11 Aug 2026 21:00:12 GMT"}, now=now,
    ) == 12
    assert retry_after_seconds({"Retry-After": "-2"}) == 0.0
    assert retry_after_seconds({"Retry-After": "999999"}) == 86400.0
    assert retry_after_seconds({"Retry-After": "not-a-date"}) is None
    assert retry_after_seconds({"Retry-After": "NaN"}) is None
