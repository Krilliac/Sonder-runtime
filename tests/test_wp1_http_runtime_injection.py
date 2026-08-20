"""Focused proof for the HTTP adapter's explicit legacy-runtime boundary."""

from pathlib import Path

import pytest

from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.interfaces.http import serve


class _Runtime:
    _STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS = 32
    FOOTER_PREFIX = "\n\n--- footer ---"

    @staticmethod
    def _strip_activity_block(value):
        return value.replace("[activity]", "")

    @staticmethod
    def _admin_account_from_token(token):
        return {"token": token}


@pytest.fixture(autouse=True)
def _unconfigured_runtime(monkeypatch):
    monkeypatch.setattr(serve, "_LEGACY_RUNTIME", None)


def test_http_import_has_no_legacy_import_or_hidden_discovery():
    source = Path(serve.__file__).read_text(encoding="utf-8")
    assert "import server" not in source
    assert "importlib" not in source


def test_legacy_access_fails_closed_until_explicitly_configured():
    with pytest.raises(DependencyUnavailable, match="configure_legacy_runtime"):
        serve.server.FOOTER_PREFIX

    runtime = _Runtime()
    assert serve.configure_legacy_runtime(runtime) is runtime
    assert serve.server.FOOTER_PREFIX == runtime.FOOTER_PREFIX
    assert serve._structured_unique_items_max_items() == 32


def test_existing_server_backed_helpers_use_the_injected_runtime():
    serve.configure_legacy_runtime(_Runtime())
    assert serve._strip_footer("answer\n\n--- footer ---[activity]") == "answer"
    assert serve._auth_account("Bearer abc") == {"token": "abc"}


def test_none_cannot_clear_or_bypass_the_runtime_boundary():
    with pytest.raises(DependencyUnavailable, match="must be supplied"):
        serve.configure_legacy_runtime(None)
