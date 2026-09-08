from dataclasses import replace

import pytest

from sonder_runtime.platform.artifact_mobility_source_config import (
    ArtifactMobilitySourceConfig,
    artifact_mobility_source_errors,
    source_scope_id,
)
from sonder_runtime.platform.config import SonderConfig


def _source(tmp_path) -> ArtifactMobilitySourceConfig:
    return ArtifactMobilitySourceConfig(
        enabled=True,
        store_dir=str(tmp_path / "private-export-spool"),
        principal_id="private-cluster",
        project_id="sonder",
        source_owner_id="node-a",
    )


def test_source_scope_depends_only_on_the_stable_source_identity(tmp_path):
    source = _source(tmp_path)

    assert source_scope_id(source) == source_scope_id(
        replace(
            source,
            store_dir=str(tmp_path / "a-different-private-spool"),
            max_object_bytes=1,
            total_bytes=1,
            ttl_seconds=1,
        )
    )
    assert source_scope_id(source) != source_scope_id(
        replace(source, source_owner_id="node-b")
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"store_dir": "relative"}, "[artifact_mobility_source].store_dir invalid"),
        ({"principal_id": "owner\nname"}, "[artifact_mobility_source].principal_id invalid"),
        ({"max_object_bytes": 0}, "[artifact_mobility_source].max_object_bytes invalid"),
        ({"total_bytes": 0}, "[artifact_mobility_source].total_bytes invalid"),
        ({"ttl_seconds": 0}, "[artifact_mobility_source].ttl_seconds invalid"),
        (
            {"max_object_bytes": 2, "total_bytes": 1},
            "[artifact_mobility_source].total_bytes invalid",
        ),
    ),
)
def test_source_configuration_fails_closed_before_a_binding_can_open(
    tmp_path, changes, expected
):
    config = SonderConfig(artifact_mobility_source=replace(_source(tmp_path), **changes))

    errors = artifact_mobility_source_errors(config)

    assert expected in errors


def test_source_scope_rejects_an_untyped_value():
    with pytest.raises(ValueError, match="^INVALID_SOURCE_SCOPE$"):
        source_scope_id(object())
