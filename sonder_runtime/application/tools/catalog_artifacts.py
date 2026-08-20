"""Deterministic runtime catalog artifacts and freshness checking.

The application catalog bundle is the sole input.  This module only renders
JSON artifacts; it has no provider, SDK, network, or filesystem discovery.
The filesystem is touched solely by the explicit write/check functions so a
composition root or CI job can choose the appropriate policy.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .generated_catalogs import CatalogBundle


ARTIFACT_NAMES = (
    "mcp.json", "openai.json", "cli.json", "client.json",
    "permissions.json", "conformance.json",
)


class CatalogArtifactDrift(ValueError):
    """Raised when generated files differ from the authoritative bundle."""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _encode(value: Any) -> str:
    return json.dumps(_plain(value), sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def render_catalog_artifacts(bundle: CatalogBundle) -> dict[str, str]:
    """Render all runtime projections plus a manifest-free artifact set."""
    if not isinstance(bundle, CatalogBundle):
        raise TypeError("bundle must be a CatalogBundle")
    return {
        "mcp.json": _encode(bundle.mcp),
        "openai.json": _encode(bundle.openai),
        "cli.json": _encode(bundle.cli),
        "client.json": _encode(bundle.client),
        "permissions.json": _encode(bundle.permissions),
        "conformance.json": _encode(bundle.conformance),
    }


def render_manifest(bundle: CatalogBundle, artifacts: Mapping[str, str]) -> str:
    """Render a digest-bound manifest for CI and clients."""
    if set(artifacts) != set(ARTIFACT_NAMES):
        raise ValueError("artifact set must contain exactly the runtime catalog files")
    files = {
        name: hashlib.sha256(artifacts[name].encode("utf-8")).hexdigest()
        for name in sorted(artifacts)
    }
    artifact_digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _encode({
        "artifact_digest": artifact_digest,
        "catalog_digest": bundle.digest,
        "files": files,
        "schema": "sonder-runtime-catalog-manifest-v1",
    })


def write_catalog_artifacts(output: str | Path, bundle: CatalogBundle) -> tuple[Path, ...]:
    """Write the complete deterministic set and return written paths."""
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    artifacts = render_catalog_artifacts(bundle)
    for name, content in artifacts.items():
        (root / name).write_text(content, encoding="utf-8", newline="\n")
    (root / "manifest.json").write_text(
        render_manifest(bundle, artifacts), encoding="utf-8", newline="\n"
    )
    return tuple(root / name for name in (*ARTIFACT_NAMES, "manifest.json"))


def check_catalog_artifacts(output: str | Path, bundle: CatalogBundle) -> tuple[str, ...]:
    """Return missing/drifted artifact names; an empty tuple proves freshness."""
    root = Path(output)
    expected = render_catalog_artifacts(bundle)
    mismatches = [
        name for name, content in expected.items()
        if not (root / name).is_file() or (root / name).read_text(encoding="utf-8") != content
    ]
    manifest = root / "manifest.json"
    expected_manifest = render_manifest(bundle, expected)
    if not manifest.is_file() or manifest.read_text(encoding="utf-8") != expected_manifest:
        mismatches.append("manifest.json")
    return tuple(mismatches)


__all__ = [
    "ARTIFACT_NAMES", "CatalogArtifactDrift", "check_catalog_artifacts",
    "render_catalog_artifacts", "render_manifest", "write_catalog_artifacts",
]
