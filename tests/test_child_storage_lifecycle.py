import io

import pytest

from sonder_runtime.bootstrap import app as composition
from sonder_runtime.bootstrap.native_mcp import run_native_mcp
from tests.test_native_mcp import _app


@pytest.mark.parametrize("failed", [False, True])
def test_native_owned_cleanup_orders_delegation_before_compute(failed):
    app = _app()
    calls = []

    def close_delegation(timeout):
        assert timeout == 5
        calls.append("delegation")
        if failed:
            raise RuntimeError("cleanup incomplete")

    app.close_delegation = close_delegation
    app.close_compute = lambda: calls.append("compute")
    for _ in range(2):
        run_native_mcp(app, input_stream=io.StringIO(""), output_stream=io.StringIO())
    assert calls == []
    if failed:
        with pytest.raises(RuntimeError, match="cleanup incomplete"):
            run_native_mcp(
                app,
                input_stream=io.StringIO(""),
                output_stream=io.StringIO(),
                close_compute_on_exit=True,
            )
    else:
        run_native_mcp(
            app,
            input_stream=io.StringIO(""),
            output_stream=io.StringIO(),
            close_compute_on_exit=True,
        )
    assert calls == ["delegation", "compute"]


@pytest.mark.parametrize("failed", [False, True])
def test_default_cleanup_never_constructs_app_and_keeps_compute_cleanup(
    monkeypatch, failed
):
    calls = []
    monkeypatch.setattr(
        composition._application_lifecycle,
        "get",
        lambda: pytest.fail("cleanup constructed an app"),
    )

    def close_delegation(timeout):
        assert 0 <= timeout <= 5
        calls.append("delegation")
        if failed:
            raise RuntimeError("cleanup incomplete")

    monkeypatch.setattr(composition, "_default_delegation_close", close_delegation)
    monkeypatch.setattr(
        composition, "_default_compute_close", lambda **kw: calls.append("compute")
    )
    if failed:
        with pytest.raises(RuntimeError, match="cleanup incomplete"):
            composition.close_default_runtime_resources()
    else:
        composition.close_default_runtime_resources()
    assert calls == ["delegation", "compute"]


def test_old_default_compute_api_remains_narrow(monkeypatch):
    calls = []
    monkeypatch.setattr(
        composition,
        "_default_delegation_close",
        lambda **kw: pytest.fail("delegation closed"),
    )
    monkeypatch.setattr(
        composition, "_default_compute_close", lambda **kw: calls.append("compute")
    )
    composition.close_default_compute()
    assert calls == ["compute"]
