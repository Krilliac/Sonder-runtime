from __future__ import annotations

import json
from pathlib import Path

from sonder_runtime.application.architecture.ownership_catalog import (
    default_layer_ownership_catalog,
)


ROOT = Path(__file__).resolve().parents[1]


def test_architecture_map_layers_do_not_depend_on_bytecode_caches(tmp_path, monkeypatch):
    # The generator listed every subdirectory of the package as a layer, so a
    # sonder_runtime/__pycache__ left by an import (CI) added a 0-file layer
    # that a PYTHONDONTWRITEBYTECODE=1 workstation could never reproduce: the
    # committed map matched one environment and the --check gate failed in
    # the other.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "generate_documentation_catalogs", ROOT / "scripts/generate_documentation_catalogs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / "sonder_runtime"
    (package / "adapters").mkdir(parents=True)
    (package / "adapters" / "thing.py").write_text("", encoding="utf-8")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"")
    (package / ".hidden").mkdir()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "PACKAGE", package)
    monkeypatch.setattr(
        module.importlib, "import_module",
        lambda name: __import__("sonder_runtime.application.architecture.ownership_catalog",
                                fromlist=["default_layer_ownership_catalog"]),
    )
    layers = [row["name"] for row in module._architecture_map()["layers"]]
    assert layers == ["adapters"]


def test_committed_architecture_map_has_no_bytecode_layer():
    generated = json.loads(
        (ROOT / "docs/architecture/generated/architecture-map.json").read_text(
            encoding="utf-8"
        )
    )
    assert "__pycache__" not in [row["name"] for row in generated["layers"]]


def test_generated_architecture_map_contains_current_layer_ownership():
    generated = json.loads(
        (ROOT / "docs/architecture/generated/architecture-map.json").read_text(
            encoding="utf-8"
        )
    )
    expected = list(
        default_layer_ownership_catalog(
            row["name"] for row in generated["layers"] if row["name"] != "__pycache__"
        ).snapshot()
    )

    assert generated["ownership"] == {
        "schema": "sonder-ownership-catalog-v1",
        "source": "sonder_runtime.application.architecture.ownership_catalog.default_layer_ownership_catalog",
        "records": expected,
    }
