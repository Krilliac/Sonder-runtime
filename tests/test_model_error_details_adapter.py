import io
import urllib.error

from sonder_runtime.adapters.model_error_details import (
    http_error_detail,
    transport_error_detail,
)


def test_http_error_detail_extracts_in_band_error_and_redacts_credentials():
    error = urllib.error.HTTPError(
        "http://localhost", 500, "server error", {},
        io.BytesIO(b'{"error": "Bearer secret-value"}'),
    )

    assert http_error_detail(error) == "Bearer=<redacted>"


def test_http_error_detail_formats_structured_body_deterministically():
    error = urllib.error.HTTPError(
        "http://localhost", 400, "bad request", {},
        io.BytesIO(b'{"z": 2, "secret": "hidden", "a": 1}'),
    )

    assert http_error_detail(error) == '{"a": 1, "secret": "<redacted>", "z": 2}'


def test_http_error_detail_falls_back_to_reason_when_body_read_fails():
    class BrokenBody:
        def read(self, _size):
            raise OSError("secret-token")

    class Error:
        code = 503
        reason = "fallback"

        def read(self, _size):
            return BrokenBody().read(_size)

    assert http_error_detail(Error()) == "fallback"


def test_transport_error_detail_uses_reason_and_redacts_it():
    error = OSError("Bearer secret-value")
    error.reason = "Bearer secret-value"

    assert transport_error_detail(error) == "Bearer=<redacted>"


def test_transport_error_detail_uses_error_when_reason_is_missing():
    assert transport_error_detail(OSError("connection refused")) == "connection refused"
