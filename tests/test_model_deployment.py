from dataclasses import FrozenInstanceError, replace

import pytest

from sonder_runtime.domain.model_deployment import ModelDeployment, ModelRank


def manifest():
    return ModelDeployment(
        cluster_id="private-a",
        deployment_id="coding",
        revision=1,
        backend="test-backend",
        backend_digest="a" * 64,
        model_bundle_digest="b" * 64,
        runtime_config_digest="c" * 64,
        context_tokens=8192,
        tensor_parallel=1,
        pipeline_parallel=2,
        reservation_group="group-1",
        ranks=(
            ModelRank(0, "host-1", "worker-wsl", "device-0"),
            ModelRank(1, "host-2", "worker-linux", "device-0"),
        ),
    )


def test_identity_covers_replacement_topology_and_artifacts():
    original = manifest()
    assert len(original.digest) == 64
    assert original.digest == manifest().digest
    for field, value in {
        "revision": 2,
        "cluster_id": "private-b",
        "backend_digest": "d" * 64,
        "model_bundle_digest": "d" * 64,
        "runtime_config_digest": "d" * 64,
        "context_tokens": 4096,
        "reservation_group": "group-2",
        "ranks": (original.ranks[0], replace(original.ranks[1], device_id="device-1")),
    }.items():
        assert replace(original, **{field: value}).digest != original.digest
    assert original.is_multihost
    assert not replace(
        original, pipeline_parallel=1, ranks=original.ranks[:1]
    ).is_multihost


def test_runtime_alias_cannot_double_count_one_physical_device():
    original = manifest()
    with pytest.raises(ValueError, match="physical device"):
        replace(
            original,
            ranks=(
                original.ranks[0],
                ModelRank(1, "host-1", "worker-windows", "device-0"),
            ),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"ranks": ()},
        {"ranks": []},
        {"tensor_parallel": 2},
        {"pipeline_parallel": True},
        {"context_tokens": True},
        {"revision": 0},
        {"backend_digest": "latest"},
        {"model_bundle_digest": "B" * 64},
        {"cluster_id": "../private"},
        {"reservation_group": ""},
        {"ranks": (ModelRank(1, "a", "w", "d"), ModelRank(0, "b", "w", "d"))},
        {"ranks": (ModelRank(0, "a", "w", "d"), object())},
    ],
)
def test_invalid_or_mutable_manifest_refused(changes):
    with pytest.raises(ValueError):
        replace(manifest(), **changes)


def test_manifest_and_rank_are_frozen_and_rank_limit_is_real():
    original = manifest()
    with pytest.raises(FrozenInstanceError):
        original.ranks[0].device_id = "changed"
    with pytest.raises(ValueError):
        replace(
            original,
            pipeline_parallel=257,
            ranks=tuple(ModelRank(i, f"host-{i}", "w", "d") for i in range(257)),
        )


@pytest.mark.parametrize("rank", [True, -1, 256, "0"])
def test_rank_requires_bounded_integer(rank):
    with pytest.raises(ValueError):
        ModelRank(rank, "h", "w", "d")
