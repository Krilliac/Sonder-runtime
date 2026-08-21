from sonder_runtime.adapters.command_catalog import CommandCatalogProvider


def test_command_catalog_provider_resolves_patched_root_module(monkeypatch):
    import command_catalog

    marker = object()
    monkeypatch.setattr(command_catalog, "http_catalog", lambda: marker)
    assert CommandCatalogProvider().http_catalog() is marker


def test_command_catalog_provider_preserves_catalog_exception_type():
    import command_catalog

    assert CommandCatalogProvider().CatalogUnavailable is command_catalog.CatalogUnavailable
