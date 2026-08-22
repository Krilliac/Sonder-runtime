from sonder_runtime.adapters.command_catalog import CommandCatalogProvider
from sonder_runtime.application.ports.command_catalog import CommandCatalog


def test_server_binds_the_canonical_dynamic_provider():
    import server
    from sonder_runtime.adapters.command_catalog import command_catalog

    assert server.command_catalog is command_catalog


def test_provider_implements_provider_neutral_catalog_contract():
    assert isinstance(CommandCatalogProvider(), CommandCatalog)


def test_root_catalog_implements_provider_neutral_catalog_contract():
    import command_catalog

    assert isinstance(command_catalog, CommandCatalog)


def test_command_catalog_provider_resolves_patched_root_module(monkeypatch):
    import command_catalog

    marker = object()
    monkeypatch.setattr(command_catalog, "http_catalog", lambda: marker)
    assert CommandCatalogProvider().http_catalog() is marker


def test_command_catalog_provider_preserves_catalog_exception_type():
    import command_catalog

    assert CommandCatalogProvider().CatalogUnavailable is command_catalog.CatalogUnavailable
