from __future__ import annotations

import json
from pathlib import Path

from sonder_runtime.application.architecture.ownership_catalog import (
    default_layer_ownership_catalog,
)


ROOT = Path(__file__).resolve().parents[1]


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
