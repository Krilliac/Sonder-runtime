import hashlib
import json
import struct
from concurrent.futures import ThreadPoolExecutor

import artifact_grounding
import model_assets


PALETTE = ((17, 15, 35), (116, 91, 218), (87, 218, 207), (49, 38, 91))


def _read_glb(path):
    payload = path.read_bytes()
    magic, version, total = struct.unpack_from("<4sII", payload, 0)
    json_length, json_kind = struct.unpack_from("<II", payload, 12)
    json_start = 20
    document = json.loads(
        payload[json_start:json_start + json_length].decode("utf-8").rstrip()
    )
    binary_header = json_start + json_length
    binary_length, binary_kind = struct.unpack_from("<II", payload, binary_header)
    binary_start = binary_header + 8
    return {
        "binary_kind": binary_kind,
        "binary_length": binary_length,
        "binary_start": binary_start,
        "document": document,
        "json_kind": json_kind,
        "magic": magic,
        "payload": payload,
        "total": total,
        "version": version,
    }


def _rewrite_document(path, parsed, document):
    json_payload = json.dumps(
        document, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    json_payload += b" " * (-len(json_payload) % 4)
    binary = parsed["payload"][
        parsed["binary_start"]:parsed["binary_start"] + parsed["binary_length"]
    ]
    binary += b"\x00" * (-len(binary) % 4)
    total = 12 + 8 + len(json_payload) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_payload), model_assets.JSON_CHUNK)
        + json_payload
        + struct.pack("<II", len(binary), model_assets.BIN_CHUNK)
        + binary
    )


def test_rigged_glb_is_self_contained_animated_gltf_2(tmp_path):
    path = tmp_path / "rigged.glb"

    stats = model_assets.write_rigged_glb(
        path, PALETTE, "arcane", 42, "Crawler", "animated crawler character"
    )
    parsed = _read_glb(path)
    document = parsed["document"]

    assert parsed["magic"] == b"glTF"
    assert parsed["version"] == 2
    assert parsed["total"] == len(parsed["payload"])
    assert parsed["json_kind"] == model_assets.JSON_CHUNK
    assert parsed["binary_kind"] == model_assets.BIN_CHUNK
    assert parsed["binary_length"] == document["buffers"][0]["byteLength"]
    assert "uri" not in document["buffers"][0]
    assert stats == {
        "animations": 1,
        "bytes": len(parsed["payload"]),
        "joints": 2,
        "triangles": 36,
        "vertices": 72,
    }
    primitive = document["meshes"][0]["primitives"][0]
    assert {"POSITION", "NORMAL", "JOINTS_0", "WEIGHTS_0"} <= set(
        primitive["attributes"]
    )
    assert document["skins"][0]["joints"] == [0, 1]
    assert document["animations"][0]["channels"][0]["target"] == {
        "node": 1,
        "path": "rotation",
    }

    grounded = artifact_grounding.validate(
        path,
        "glb",
        {
            "min_vertices": 72,
            "min_triangles": 36,
            "min_joints": 2,
            "min_animations": 1,
            "no_external_dependencies": True,
            "required_text": ["Crawler", "ShellPulse"],
        },
    )
    assert grounded["ok"], artifact_grounding.format_result(grounded)


def test_rigged_glb_is_deterministic_and_seed_sensitive(tmp_path):
    first = tmp_path / "first.glb"
    second = tmp_path / "second.glb"
    changed = tmp_path / "changed.glb"

    model_assets.write_rigged_glb(first, PALETTE, "frost", 77, "Rig", "brief")
    model_assets.write_rigged_glb(second, PALETTE, "frost", 77, "Rig", "brief")
    model_assets.write_rigged_glb(changed, PALETTE, "frost", 78, "Rig", "brief")

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    assert digest(first) == digest(second)
    assert digest(first) != digest(changed)


def test_rigged_glb_sanitizes_invalid_user_text(tmp_path):
    path = tmp_path / "sanitized.glb"

    model_assets.write_rigged_glb(
        path,
        PALETTE,
        "arcane\ud800",
        42,
        "Crawler\ufffe",
        "brief\x01 with invalid \udfff text",
    )

    serialized = json.dumps(_read_glb(path)["document"], ensure_ascii=True)
    assert "\\ud800" not in serialized
    assert "\\udfff" not in serialized
    assert "\\ufffe" not in serialized
    assert "\\u0001" not in serialized


def test_rigged_glb_atomic_write_survives_parallel_same_target(tmp_path):
    expected = tmp_path / "expected.glb"
    shared = tmp_path / "shared.glb"
    model_assets.write_rigged_glb(expected, PALETTE, "arcane", 91, "Rig", "brief")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                model_assets.write_rigged_glb,
                shared,
                PALETTE,
                "arcane",
                91,
                "Rig",
                "brief",
            )
            for _index in range(16)
        ]
        for future in futures:
            future.result()

    assert shared.read_bytes() == expected.read_bytes()
    assert not list(tmp_path.glob("*.tmp"))
    result = artifact_grounding.validate(
        shared, "glb", {"min_joints": 2, "min_animations": 1}
    )
    assert result["ok"], artifact_grounding.format_result(result)


def test_glb_grounding_rejects_container_length_tampering(tmp_path):
    path = tmp_path / "broken.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    payload = bytearray(path.read_bytes())
    struct.pack_into("<I", payload, 8, len(payload) + 4)
    path.write_bytes(payload)

    result = artifact_grounding.validate(path, "auto")

    assert not result["ok"]
    assert next(check for check in result["checks"] if check["name"] == "valid-glb")["ok"] is False


def test_glb_grounding_rejects_out_of_range_joint_data(tmp_path):
    path = tmp_path / "broken-joints.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    joint_accessor = next(
        accessor for accessor in document["accessors"] if accessor.get("name") == "JOINTS_0"
    )
    view = document["bufferViews"][joint_accessor["bufferView"]]
    payload = bytearray(parsed["payload"])
    payload[parsed["binary_start"] + view["byteOffset"]] = 255
    path.write_bytes(payload)

    result = artifact_grounding.validate(
        path, "glb", {"min_joints": 2, "min_animations": 1}
    )

    assert not result["ok"]
    skinning = next(
        check for check in result["checks"] if check["name"] == "glb-skinning"
    )
    assert not skinning["ok"]
    assert "out-of-range joint" in skinning["detail"]


def test_glb_grounding_rejects_non_unit_animation_quaternion(tmp_path):
    path = tmp_path / "broken-animation.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    rotation_accessor = next(
        accessor
        for accessor in document["accessors"]
        if accessor.get("name") == "SHELL_ROTATION"
    )
    view = document["bufferViews"][rotation_accessor["bufferView"]]
    payload = bytearray(parsed["payload"])
    struct.pack_into(
        "<4f",
        payload,
        parsed["binary_start"] + view["byteOffset"],
        0.0,
        0.0,
        0.0,
        0.0,
    )
    path.write_bytes(payload)

    result = artifact_grounding.validate(
        path, "glb", {"min_joints": 2, "min_animations": 1}
    )

    assert not result["ok"]
    animation = next(
        check for check in result["checks"] if check["name"] == "glb-animations"
    )
    assert not animation["ok"]
    assert "unit quaternions" in animation["detail"]


def test_glb_grounding_fails_closed_on_malformed_schema_fields(tmp_path):
    path = tmp_path / "malformed.glb"

    def bad_buffer(document):
        document["buffers"][0]["byteLength"] = "large"

    def bad_joints(document):
        document["skins"][0]["joints"] = [{}]

    def bad_sampler(document):
        document["animations"][0]["samplers"][0] = None

    def bad_images(document):
        document["images"] = None

    for mutate in (bad_buffer, bad_joints, bad_sampler, bad_images):
        model_assets.write_rigged_glb(path, PALETTE)
        parsed = _read_glb(path)
        document = parsed["document"]
        mutate(document)
        _rewrite_document(path, parsed, document)

        result = artifact_grounding.validate(
            path,
            "glb",
            {
                "min_joints": 2,
                "min_animations": 1,
                "no_external_dependencies": True,
            },
        )

        assert not result["ok"]
