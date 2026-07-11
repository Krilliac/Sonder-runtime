import hashlib
import json
import struct
import zlib
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
    declared_binary = document["buffers"][0]["byteLength"]
    assert declared_binary <= parsed["binary_length"] <= declared_binary + 3
    assert "uri" not in document["buffers"][0]
    assert stats == {
        "animation_sequences": 2,
        "animations": 6,
        "bytes": len(parsed["payload"]),
        "images": 3,
        "joints": 17,
        "materials": 1,
        "morph_targets": 2,
        "textures": 3,
        "triangles": 192,
        "vertices": 384,
    }
    primitive = document["meshes"][0]["primitives"][0]
    assert {
        "POSITION", "NORMAL", "TANGENT", "TEXCOORD_0", "JOINTS_0", "WEIGHTS_0",
    } <= set(
        primitive["attributes"]
    )
    assert len(document["images"]) == 3
    assert len(document["textures"]) == 3
    assert all("uri" not in image for image in document["images"])
    for image in document["images"]:
        view = document["bufferViews"][image["bufferView"]]
        start = parsed["binary_start"] + view["byteOffset"]
        assert parsed["payload"][start:start + 8] == b"\x89PNG\r\n\x1a\n"
    pbr = document["materials"][0]["pbrMetallicRoughness"]
    assert pbr["baseColorTexture"] == {"index": 0}
    assert pbr["metallicRoughnessTexture"] == {"index": 1}
    assert document["materials"][0]["occlusionTexture"]["index"] == 1
    assert document["materials"][0]["normalTexture"]["index"] == 2
    assert len(primitive["targets"]) == 2
    assert all(set(target) == {"POSITION", "NORMAL", "TANGENT"} for target in primitive["targets"])
    assert document["meshes"][0]["extras"]["targetNames"] == ["Breathe", "Focus"]
    assert document["nodes"][17]["weights"] == [0.0, 0.0]
    assert document["skins"][0]["joints"] == list(range(17))
    assert document["animations"][0]["channels"][0]["target"] == {
        "node": 2,
        "path": "rotation",
    }
    idle_translation_index = next(
        index
        for index, accessor in enumerate(document["accessors"])
        if accessor.get("name") == "IDLE_HIPS_TRANSLATION"
    )
    binary = parsed["payload"][
        parsed["binary_start"]:parsed["binary_start"] + parsed["binary_length"]
    ]
    idle_translations = artifact_grounding._glb_accessor_values(
        document, binary, idle_translation_index
    )
    assert idle_translations[0] == (0.0, 1.0, 0.0)
    assert idle_translations[1][1] > 1.0
    assert idle_translations[2] == (0.0, 1.0, 0.0)
    assert {animation["name"] for animation in document["animations"]} == {
        "Idle",
        "Walk",
        "Run",
        "Wave",
        "Breathe",
        "Focus",
    }
    assert document["extras"]["humanoidBones"]["Head"] == 4
    assert {sequence["name"] for sequence in document["extras"]["animationSequences"]} == {
        "AmbientCharacter",
        "LocomotionRamp",
    }

    grounded = artifact_grounding.validate(
        path,
        "glb",
        {
            "min_vertices": 384,
            "min_triangles": 192,
            "min_joints": 17,
            "min_animations": 6,
            "min_skeletal_animations": 4,
            "min_morph_animations": 2,
            "min_morph_targets": 2,
            "min_animation_sequences": 2,
            "min_images": 3,
            "min_materials": 1,
            "min_textures": 3,
            "min_texcoord_sets": 1,
            "no_external_dependencies": True,
            "require_embedded_images": True,
            "require_material_textures": True,
            "require_named_animations": True,
            "require_named_morph_targets": True,
            "require_morph_normals": True,
            "require_morph_tangents": True,
            "require_humanoid_rig": True,
            "require_animation_clip_metadata": True,
            "require_power_of_two_images": True,
            "require_tangents": True,
            "required_animation_clips": ["Idle", "Walk", "Run", "Breathe", "Focus"],
            "required_text": ["Crawler", "LocomotionRamp"],
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
        if accessor.get("name") == "IDLE_CHEST_ROTATION"
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


def test_glb_grounding_rejects_corrupt_embedded_texture(tmp_path):
    path = tmp_path / "broken-texture.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    image = parsed["document"]["images"][0]
    view = parsed["document"]["bufferViews"][image["bufferView"]]
    payload = bytearray(parsed["payload"])
    png_start = parsed["binary_start"] + view["byteOffset"]
    chunk = png_start + 8
    while payload[chunk + 4:chunk + 8] != b"IDAT":
        chunk += 12 + struct.unpack_from(">I", payload, chunk)[0]
    chunk_length = struct.unpack_from(">I", payload, chunk)[0]
    data_start = chunk + 8
    data_end = data_start + chunk_length
    payload[data_start] ^= 0xFF
    crc = zlib.crc32(payload[chunk + 4:data_end]) & 0xFFFFFFFF
    struct.pack_into(">I", payload, data_end, crc)
    path.write_bytes(payload)

    result = artifact_grounding.validate(
        path,
        "glb",
        {
            "min_images": 2,
            "require_embedded_images": True,
            "require_material_textures": True,
        },
    )

    assert not result["ok"]
    images = next(check for check in result["checks"] if check["name"] == "glb-images")
    assert not images["ok"]
    assert "embedded PNG" in images["detail"]


def test_glb_grounding_rejects_invalid_texture_reference(tmp_path):
    path = tmp_path / "broken-reference.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    document["textures"][0]["source"] = 999
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path,
        "glb",
        {"min_textures": 2, "require_material_textures": True},
    )

    assert not result["ok"]
    materials = next(
        check for check in result["checks"] if check["name"] == "glb-materials"
    )
    assert not materials["ok"]
    assert "invalid source" in materials["detail"]


def test_glb_grounding_requires_material_texture_coordinates(tmp_path):
    path = tmp_path / "missing-uv.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    del document["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"]
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path,
        "glb",
        {
            "min_texcoord_sets": 1,
            "require_material_textures": True,
        },
    )

    assert not result["ok"]
    texcoords = next(
        check
        for check in result["checks"]
        if check["name"] == "glb-texture-coordinates"
    )
    assert not texcoords["ok"]
    assert "TEXCOORD_0 is missing" in texcoords["detail"]


def test_glb_grounding_requires_normal_map_tangents(tmp_path):
    path = tmp_path / "missing-tangent.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    del document["meshes"][0]["primitives"][0]["attributes"]["TANGENT"]
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path,
        "glb",
        {
            "require_material_textures": True,
            "require_tangents": True,
        },
    )

    assert not result["ok"]
    tangents = next(
        check for check in result["checks"] if check["name"] == "glb-tangents"
    )
    assert not tangents["ok"]
    assert "TANGENT is missing" in tangents["detail"]


def test_glb_grounding_rejects_morph_delta_count_mismatch(tmp_path):
    path = tmp_path / "broken-morph-count.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    morph_accessor = next(
        accessor
        for accessor in document["accessors"]
        if accessor.get("name") == "MORPH_BREATHE_POSITION"
    )
    morph_accessor["count"] -= 1
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path,
        "glb",
        {"min_morph_targets": 1, "require_named_morph_targets": True},
    )

    assert not result["ok"]
    morphs = next(
        check for check in result["checks"] if check["name"] == "glb-morph-targets"
    )
    assert not morphs["ok"]
    assert "delta count must match POSITION" in morphs["detail"]


def test_glb_grounding_requires_named_morph_targets(tmp_path):
    path = tmp_path / "unnamed-morph.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    del document["meshes"][0]["extras"]
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path,
        "glb",
        {"min_morph_targets": 1, "require_named_morph_targets": True},
    )

    assert not result["ok"]
    morphs = next(
        check for check in result["checks"] if check["name"] == "glb-morph-targets"
    )
    assert not morphs["ok"]


def test_glb_grounding_rejects_morph_animation_output_count_mismatch(tmp_path):
    path = tmp_path / "broken-morph-animation.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    output = next(
        accessor
        for accessor in document["accessors"]
        if accessor.get("name") == "MORPH_BREATHE_WEIGHT"
    )
    output["count"] -= 1
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path,
        "glb",
        {"min_animations": 3, "min_morph_animations": 1},
    )

    assert not result["ok"]
    animations = next(
        check for check in result["checks"] if check["name"] == "glb-animations"
    )
    assert not animations["ok"]
    assert "input/output counts do not match" in animations["detail"]


def test_glb_grounding_rejects_duplicate_animation_names(tmp_path):
    path = tmp_path / "duplicate-animation.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    document["animations"][1]["name"] = document["animations"][0]["name"]
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path,
        "glb",
        {"min_animations": 3, "require_named_animations": True},
    )

    assert not result["ok"]
    animations = next(
        check for check in result["checks"] if check["name"] == "glb-animations"
    )
    assert not animations["ok"]
    assert "duplicated" in animations["detail"]


def test_glb_grounding_requires_complete_morph_frame_semantics(tmp_path):
    path = tmp_path / "missing-morph-tangent.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    del document["meshes"][0]["primitives"][0]["targets"][0]["TANGENT"]
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path,
        "glb",
        {
            "min_morph_targets": 2,
            "require_morph_normals": True,
            "require_morph_tangents": True,
        },
    )

    assert not result["ok"]
    morphs = next(
        check for check in result["checks"] if check["name"] == "glb-morph-targets"
    )
    assert not morphs["ok"]


def test_glb_grounding_rejects_nonorthogonal_morphed_tangent_frame(tmp_path):
    path = tmp_path / "broken-morph-frame.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    accessor = next(
        item
        for item in parsed["document"]["accessors"]
        if item.get("name") == "MORPH_BREATHE_NORMAL"
    )
    view = parsed["document"]["bufferViews"][accessor["bufferView"]]
    payload = bytearray(parsed["payload"])
    struct.pack_into(
        "<3f",
        payload,
        parsed["binary_start"] + view["byteOffset"],
        0.0,
        0.0,
        1.0,
    )
    path.write_bytes(payload)

    result = artifact_grounding.validate(
        path,
        "glb",
        {"require_morph_normals": True, "require_morph_tangents": True},
    )

    assert not result["ok"]
    morphs = next(
        check for check in result["checks"] if check["name"] == "glb-morph-targets"
    )
    assert "orthogonal" in morphs["detail"]


def test_glb_grounding_rejects_broken_humanoid_hierarchy(tmp_path):
    path = tmp_path / "broken-humanoid.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    document["nodes"][5]["children"].remove(6)
    document["nodes"][2]["children"].append(6)
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path, "glb", {"min_joints": 17, "require_humanoid_rig": True}
    )

    assert not result["ok"]
    humanoid = next(
        check for check in result["checks"] if check["name"] == "glb-humanoid-rig"
    )
    assert "LeftElbow must be a direct child" in humanoid["detail"]


def test_glb_grounding_rejects_non_affine_inverse_bind_matrix(tmp_path):
    path = tmp_path / "broken-bind.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    accessor = next(
        item
        for item in parsed["document"]["accessors"]
        if item.get("name") == "INVERSE_BIND_MATRICES"
    )
    view = parsed["document"]["bufferViews"][accessor["bufferView"]]
    payload = bytearray(parsed["payload"])
    struct.pack_into(
        "<f", payload, parsed["binary_start"] + view["byteOffset"] + 12, 1.0
    )
    path.write_bytes(payload)

    result = artifact_grounding.validate(path, "glb", {"min_joints": 17})

    assert not result["ok"]
    skinning = next(
        check for check in result["checks"] if check["name"] == "glb-skinning"
    )
    assert "finite affine MAT4" in skinning["detail"]


def test_glb_grounding_rejects_unknown_clip_in_sequence(tmp_path):
    path = tmp_path / "broken-sequence.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    document["extras"]["animationSequences"][0]["clips"][2] = "Teleport"
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path,
        "glb",
        {
            "min_animation_sequences": 2,
            "require_animation_clip_metadata": True,
        },
    )

    assert not result["ok"]
    sequences = next(
        check
        for check in result["checks"]
        if check["name"] == "glb-animation-sequences"
    )
    assert "invalid clips" in sequences["detail"]


def test_glb_grounding_rejects_clip_duration_drift(tmp_path):
    path = tmp_path / "broken-clip-duration.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    document["extras"]["animationClips"][0]["duration"] = 9.0
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path, "glb", {"require_animation_clip_metadata": True}
    )

    assert not result["ok"]
    sequences = next(
        check
        for check in result["checks"]
        if check["name"] == "glb-animation-sequences"
    )
    assert "invalid duration" in sequences["detail"]


def test_glb_grounding_fails_closed_on_non_string_clip_name(tmp_path):
    path = tmp_path / "broken-clip-name.glb"
    model_assets.write_rigged_glb(path, PALETTE)
    parsed = _read_glb(path)
    document = parsed["document"]
    document["extras"]["animationClips"][0]["name"] = ["Idle"]
    _rewrite_document(path, parsed, document)

    result = artifact_grounding.validate(
        path, "glb", {"require_animation_clip_metadata": True}
    )

    assert not result["ok"]
    sequences = next(
        check
        for check in result["checks"]
        if check["name"] == "glb-animation-sequences"
    )
    assert "invalid name" in sequences["detail"]


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

    def bad_textures(document):
        document["textures"] = None

    def bad_texture_sampler(document):
        document["samplers"] = [{"wrapS": "repeat"}]

    def bad_morph_accessor(document):
        document["meshes"][0]["primitives"][0]["targets"][0]["POSITION"] = True

    def negative_morph_accessor(document):
        document["meshes"][0]["primitives"][0]["targets"][0]["POSITION"] = -1

    def bad_animation_accessors(document):
        document["animations"][0]["samplers"][0]["input"] = True

    def negative_animation_accessor(document):
        document["animations"][0]["samplers"][0]["input"] = -1

    for mutate in (
        bad_buffer,
        bad_joints,
        bad_sampler,
        bad_images,
        bad_textures,
        bad_texture_sampler,
        bad_morph_accessor,
        negative_morph_accessor,
        bad_animation_accessors,
        negative_animation_accessor,
    ):
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
