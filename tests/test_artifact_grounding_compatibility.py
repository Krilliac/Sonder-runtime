"""Packaging and compatibility ratchets for artifact grounding."""

import importlib
from pathlib import Path


def test_packaged_artifact_grounding_owns_the_implementation():
    packaged = importlib.import_module("sonder_runtime.adapters.artifact_grounding")

    assert packaged.__file__.replace("\\", "/").endswith(
        "sonder_runtime/adapters/artifact_grounding.py"
    )
    assert callable(packaged.parse_requirements)
    assert callable(packaged.validate)
    assert callable(packaged.format_result)


def test_root_artifact_grounding_is_an_identity_compatibility_alias():
    packaged = importlib.import_module("sonder_runtime.adapters.artifact_grounding")
    legacy = importlib.import_module("artifact_grounding")

    assert legacy is packaged
    assert legacy._validate_directory is packaged._validate_directory
    assert legacy._is_reparse_point is packaged._is_reparse_point


def test_server_and_assetgen_bind_the_packaged_adapter_directly():
    repository_root = Path(__file__).resolve().parents[1]
    server_source = (repository_root / "server.py").read_text(encoding="utf-8")
    assetgen_source = (repository_root / "assetgen.py").read_text(encoding="utf-8")

    assert (
        "import sonder_runtime.adapters.artifact_grounding as artifact_grounding"
        in server_source
    )
    assert (
        "import sonder_runtime.adapters.artifact_grounding as artifact_grounding"
        in assetgen_source
    )
    assert "import artifact_grounding" not in server_source
    assert "import artifact_grounding" not in assetgen_source

