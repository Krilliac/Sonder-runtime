"""Deterministic textured, rigged, morphing, multi-clip glTF 2.0 generation."""

from __future__ import annotations

import json
import math
import os
import struct
import tempfile
import time
import zlib


GLB_MAGIC = b"glTF"
GLB_VERSION = 2
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942


def _clean(value, limit=240):
    normalized = " ".join(str(value or "").replace("\x00", " ").split())
    safe = "".join(
        character
        for character in normalized
        if ord(character) >= 0x20
        and not 0xD800 <= ord(character) <= 0xDFFF
        and ord(character) not in {0xFFFE, 0xFFFF}
    )
    return safe[:limit] or "Trilobite rigged model"


def _atomic_write_bytes(path, payload):
    destination = os.path.abspath(path)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=os.path.basename(destination) + ".",
        suffix=".tmp",
        dir=os.path.dirname(destination),
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(12):
            try:
                os.replace(temporary, destination)
                break
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(0.005 * (attempt + 1))
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


class _BufferBuilder:
    def __init__(self):
        self.payload = bytearray()
        self.views = []

    def add(self, payload, target=None):
        self.payload.extend(b"\x00" * (-len(self.payload) % 4))
        offset = len(self.payload)
        self.payload.extend(payload)
        view = {
            "buffer": 0,
            "byteLength": len(payload),
            "byteOffset": offset,
        }
        if target is not None:
            view["target"] = target
        self.views.append(view)
        return len(self.views) - 1


def _float_payload(values):
    return struct.pack("<%df" % len(values), *values)


def _flatten(rows):
    return [value for row in rows for value in row]


def _png_chunk(kind, payload):
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png_rgba(width, height, pixels):
    if len(pixels) != width * height * 4:
        raise ValueError("RGBA texture payload has the wrong size")
    stride = width * 4
    rows = b"".join(
        b"\x00" + pixels[y * stride:(y + 1) * stride]
        for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(rows, 9))
        + _png_chunk(b"IEND", b"")
    )


def _mix(left, right, amount):
    return tuple(
        max(0, min(255, round(a + (b - a) * amount)))
        for a, b in zip(left, right)
    )


def _material_textures(palette, seed, size=32):
    color = bytearray()
    surface = bytearray()
    normal = bytearray()
    phase = int(seed) & 15
    for y in range(size):
        for x in range(size):
            checker = ((x // 4) + (y // 4) + phase) & 1
            ridge = ((x + y + phase) % 11) < 2
            base = _mix(palette[1], palette[2], 0.38 if checker else 0.08)
            if ridge:
                base = _mix(base, palette[2], 0.55)
            color.extend((*base, 255))
            occlusion = 210 + (22 if checker else 0)
            roughness = 150 + ((x * 5 + y * 3 + phase) % 70)
            metallic = 96 + (72 if ridge else 0)
            surface.extend((occlusion, roughness, metallic, 255))
            nx = 0.16 * math.sin((x + phase) * 0.55)
            ny = 0.16 * math.cos((y - phase) * 0.47)
            nz = math.sqrt(max(0.0, 1.0 - nx * nx - ny * ny))
            normal.extend(
                (
                    round((nx * 0.5 + 0.5) * 255),
                    round((ny * 0.5 + 0.5) * 255),
                    round((nz * 0.5 + 0.5) * 255),
                    255,
                )
            )
    return (
        _png_rgba(size, size, bytes(color)),
        _png_rgba(size, size, bytes(surface)),
        _png_rgba(size, size, bytes(normal)),
    )


def _append_box(
    positions, normals, tangents, texcoords, joints, weights, indices, bounds, skin,
):
    x0, y0, z0, x1, y1, z1 = bounds
    faces = (
        ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))),
        ((-1.0, 0.0, 0.0), (0.0, 0.0, -1.0), ((x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (x0, y0, z0))),
        ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), ((x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0))),
        ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), ((x0, y0, z1), (x0, y0, z0), (x1, y0, z0), (x1, y0, z1))),
        ((0.0, 0.0, 1.0), (-1.0, 0.0, 0.0), ((x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (x0, y0, z1))),
        ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0), ((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0))),
    )
    face_uvs = ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0))
    for normal, tangent, corners in faces:
        base = len(positions)
        for position, uv in zip(corners, face_uvs):
            positions.append(position)
            normals.append(normal)
            tangents.append((*tangent, -1.0))
            texcoords.append(uv)
            joint_row, weight_row = skin(position)
            joints.append(joint_row)
            weights.append(weight_row)
        indices.extend((base, base + 1, base + 2, base, base + 2, base + 3))


def _identity_matrix():
    return (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )


def _inverse_translation(x, y, z):
    return (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        -x, -y, -z, 1.0,
    )


def _quaternion(axis, degrees):
    angle = math.radians(degrees) * 0.5
    sine = math.sin(angle)
    x, y, z = axis
    return (
        round(x * sine, 7),
        round(y * sine, 7),
        round(z * sine, 7),
        round(math.cos(angle), 7),
    )


def _rotated_delta(vector, axis, angle):
    x, y, z = vector
    cosine = math.cos(angle)
    sine = math.sin(angle)
    if axis == "x":
        rotated = (x, y * cosine - z * sine, y * sine + z * cosine)
    else:
        rotated = (x * cosine + z * sine, y, -x * sine + z * cosine)
    return tuple(round(changed - original, 7) for changed, original in zip(rotated, vector))


def write_rigged_glb(path, palette, theme="arcane", seed=1337, title="Trilobite", brief=""):
    """Write a self-contained humanoid GLB with PBR, skinning, morphs, and clips."""
    seed = int(seed)
    variation = ((seed & 255) / 255.0 - 0.5) * 0.16
    shoulder_x = round(0.45 + variation * 0.12, 6)
    hip_x = round(0.24 + variation * 0.08, 6)
    head_top = round(2.62 + variation * 0.2, 6)

    joint_specs = (
        ("Hips", None, (0.0, 1.0, 0.0)),
        ("Spine", 0, (0.0, 1.34, 0.0)),
        ("Chest", 1, (0.0, 1.76, 0.0)),
        ("Neck", 2, (0.0, 2.08, 0.0)),
        ("Head", 3, (0.0, 2.34, 0.0)),
        ("LeftShoulder", 2, (-shoulder_x, 1.86, 0.0)),
        ("LeftElbow", 5, (-0.88, 1.86, 0.0)),
        ("LeftWrist", 6, (-1.25, 1.86, 0.0)),
        ("RightShoulder", 2, (shoulder_x, 1.86, 0.0)),
        ("RightElbow", 8, (0.88, 1.86, 0.0)),
        ("RightWrist", 9, (1.25, 1.86, 0.0)),
        ("LeftHip", 0, (-hip_x, 0.92, 0.0)),
        ("LeftKnee", 11, (-hip_x, 0.48, 0.0)),
        ("LeftAnkle", 12, (-hip_x, 0.1, 0.0)),
        ("RightHip", 0, (hip_x, 0.92, 0.0)),
        ("RightKnee", 14, (hip_x, 0.48, 0.0)),
        ("RightAnkle", 15, (hip_x, 0.1, 0.0)),
    )

    positions = []
    normals = []
    tangents = []
    texcoords = []
    joints = []
    weights = []
    indices = []

    def fixed_skin(index):
        def skin(_position):
            return (index, 0, 0, 0), (1.0, 0.0, 0.0, 0.0)
        return skin

    def pair_skin(first, second, axis, start, end):
        def skin(position):
            amount = (position[axis] - start) / (end - start)
            amount = round(max(0.0, min(1.0, amount)), 6)
            first_weight = round(1.0 - amount, 6)
            return (
                (
                    first if first_weight > 0.0 else 0,
                    second if amount > 0.0 else 0,
                    0,
                    0,
                ),
                (first_weight, amount, 0.0, 0.0),
            )
        return skin

    _append_box(
        positions, normals, tangents, texcoords, joints, weights, indices,
        (-0.38, 0.82, -0.23, 0.38, 1.18, 0.23),
        pair_skin(0, 1, 1, 0.86, 1.18),
    )
    _append_box(
        positions, normals, tangents, texcoords, joints, weights, indices,
        (-0.48 - variation * 0.25, 1.14, -0.25, 0.48 + variation * 0.25, 1.96, 0.25),
        pair_skin(1, 2, 1, 1.22, 1.84),
    )
    _append_box(
        positions, normals, tangents, texcoords, joints, weights, indices,
        (-0.15, 1.96, -0.15, 0.15, 2.2, 0.15),
        pair_skin(3, 4, 1, 1.98, 2.2),
    )
    _append_box(
        positions, normals, tangents, texcoords, joints, weights, indices,
        (-0.28, 2.16, -0.25, 0.28, head_top, 0.25), fixed_skin(4),
    )
    for side, shoulder, elbow, wrist in ((-1, 5, 6, 7), (1, 8, 9, 10)):
        inner = side * shoulder_x
        elbow_x = side * 0.88
        wrist_x = side * 1.25
        outer = side * 1.42
        x0, x1 = sorted((inner, elbow_x))
        _append_box(
            positions, normals, tangents, texcoords, joints, weights, indices,
            (x0, 1.72, -0.14, x1, 1.98, 0.14),
            pair_skin(shoulder, elbow, 0, inner, elbow_x),
        )
        x0, x1 = sorted((elbow_x, wrist_x))
        _append_box(
            positions, normals, tangents, texcoords, joints, weights, indices,
            (x0, 1.74, -0.12, x1, 1.96, 0.12),
            pair_skin(elbow, wrist, 0, elbow_x, wrist_x),
        )
        x0, x1 = sorted((wrist_x, outer))
        _append_box(
            positions, normals, tangents, texcoords, joints, weights, indices,
            (x0, 1.72, -0.14, x1, 1.98, 0.14), fixed_skin(wrist),
        )
    for side, hip, knee, ankle in ((-1, 11, 12, 13), (1, 14, 15, 16)):
        center = side * hip_x
        _append_box(
            positions, normals, tangents, texcoords, joints, weights, indices,
            (center - 0.15, 0.46, -0.16, center + 0.15, 0.96, 0.16),
            pair_skin(hip, knee, 1, 0.92, 0.48),
        )
        _append_box(
            positions, normals, tangents, texcoords, joints, weights, indices,
            (center - 0.13, 0.08, -0.14, center + 0.13, 0.48, 0.14),
            pair_skin(knee, ankle, 1, 0.46, 0.1),
        )
        _append_box(
            positions, normals, tangents, texcoords, joints, weights, indices,
            (center - 0.15, 0.0, -0.16, center + 0.15, 0.16, 0.4),
            fixed_skin(ankle),
        )

    builder = _BufferBuilder()
    accessors = []

    def accessor(payload, component_type, count, value_type, name, target=None,
                 minimum=None, maximum=None):
        view = builder.add(payload, target)
        item = {
            "bufferView": view,
            "componentType": component_type,
            "count": count,
            "name": name,
            "type": value_type,
        }
        if minimum is not None:
            item["min"] = list(minimum)
        if maximum is not None:
            item["max"] = list(maximum)
        accessors.append(item)
        return len(accessors) - 1

    position_values = _flatten(positions)
    normal_values = _flatten(normals)
    tangent_values = _flatten(tangents)
    texcoord_values = _flatten(texcoords)
    joint_values = _flatten(joints)
    weight_values = _flatten(weights)
    morph_rows = []
    for morph_name in ("BREATHE", "FOCUS"):
        position_deltas = []
        normal_deltas = []
        tangent_deltas = []
        for position, normal, tangent in zip(positions, normals, tangents):
            x, y, z = position
            if morph_name == "BREATHE":
                vertical = max(0.0, min(1.0, (y - 1.12) / 1.05))
                central = max(0.0, min(1.0, 1.0 - abs(x) / 1.0))
                influence = vertical * central
                angle = 0.045 * influence
                position_delta = (
                    round((x * 0.045 + z * 0.035) * influence, 7),
                    round(0.025 * influence, 7),
                    round((z * 0.045 - x * 0.035) * influence, 7),
                )
                axis = "y"
            else:
                influence = max(0.0, min(1.0, (y - 2.08) / 0.32))
                angle = -0.07 * influence
                position_delta = (
                    round(x * 0.025 * influence, 7),
                    round(-0.04 * influence, 7),
                    round((y - 2.08) * 0.055 * influence, 7),
                )
                axis = "x"
            position_deltas.append(position_delta)
            normal_deltas.append(_rotated_delta(normal, axis, angle))
            tangent_deltas.append(_rotated_delta(tangent[:3], axis, angle))
        morph_rows.append(
            (morph_name, position_deltas, normal_deltas, tangent_deltas)
        )
    mins = [min(row[axis] for row in positions) for axis in range(3)]
    maxs = [max(row[axis] for row in positions) for axis in range(3)]
    position_accessor = accessor(
        _float_payload(position_values), 5126, len(positions), "VEC3", "POSITION",
        34962, mins, maxs,
    )
    normal_accessor = accessor(
        _float_payload(normal_values), 5126, len(normals), "VEC3", "NORMAL", 34962,
    )
    tangent_accessor = accessor(
        _float_payload(tangent_values), 5126, len(tangents), "VEC4", "TANGENT", 34962,
    )
    texcoord_accessor = accessor(
        _float_payload(texcoord_values), 5126, len(texcoords), "VEC2", "TEXCOORD_0", 34962,
    )
    joint_accessor = accessor(
        bytes(joint_values), 5121, len(joints), "VEC4", "JOINTS_0", 34962,
    )
    weight_accessor = accessor(
        _float_payload(weight_values), 5126, len(weights), "VEC4", "WEIGHTS_0", 34962,
    )
    index_accessor = accessor(
        struct.pack("<%dH" % len(indices), *indices),
        5123, len(indices), "SCALAR", "INDICES", 34963,
        [min(indices)], [max(indices)],
    )
    morph_targets = []
    for morph_name, position_deltas, normal_deltas, tangent_deltas in morph_rows:
        morph_mins = [min(row[axis] for row in position_deltas) for axis in range(3)]
        morph_maxs = [max(row[axis] for row in position_deltas) for axis in range(3)]
        position_delta_accessor = accessor(
            _float_payload(_flatten(position_deltas)), 5126, len(position_deltas),
            "VEC3", "MORPH_%s_POSITION" % morph_name, 34962,
            morph_mins, morph_maxs,
        )
        normal_delta_accessor = accessor(
            _float_payload(_flatten(normal_deltas)), 5126, len(normal_deltas),
            "VEC3", "MORPH_%s_NORMAL" % morph_name, 34962,
        )
        tangent_delta_accessor = accessor(
            _float_payload(_flatten(tangent_deltas)), 5126, len(tangent_deltas),
            "VEC3", "MORPH_%s_TANGENT" % morph_name, 34962,
        )
        morph_targets.append({
            "NORMAL": normal_delta_accessor,
            "POSITION": position_delta_accessor,
            "TANGENT": tangent_delta_accessor,
        })
    matrices = tuple(
        value
        for _name, _parent, translation in joint_specs
        for value in _inverse_translation(*translation)
    )
    bind_accessor = accessor(
        _float_payload(matrices), 5126, len(joint_specs), "MAT4",
        "INVERSE_BIND_MATRICES",
    )
    short_times = (0.0, 0.5, 1.0)
    cycle_times = (0.0, 0.25, 0.5, 0.75, 1.0)
    short_time_accessor = accessor(
        _float_payload(short_times), 5126, len(short_times), "SCALAR",
        "ANIMATION_TIME_SHORT", minimum=[0.0], maximum=[1.0],
    )
    cycle_time_accessor = accessor(
        _float_payload(cycle_times), 5126, len(cycle_times), "SCALAR",
        "ANIMATION_TIME_CYCLE", minimum=[0.0], maximum=[1.0],
    )

    def rotation_accessor(name, axis, degrees):
        rows = [_quaternion(axis, value) for value in degrees]
        return accessor(
            _float_payload(_flatten(rows)), 5126, len(rows), "VEC4", name,
        )

    idle_rotation = rotation_accessor(
        "IDLE_CHEST_ROTATION", (0.0, 0.0, 1.0), (0.0, 2.5, 0.0),
    )
    idle_translations = (
        (0.0, 1.0, 0.0),
        (0.0, round(1.025 + abs(variation) * 0.03, 7), 0.0),
        (0.0, 1.0, 0.0),
    )
    idle_translation = accessor(
        _float_payload(_flatten(idle_translations)), 5126,
        len(idle_translations), "VEC3", "IDLE_HIPS_TRANSLATION",
    )
    walk_left_hip = rotation_accessor(
        "WALK_LEFT_HIP", (1.0, 0.0, 0.0), (22.0, 0.0, -22.0, 0.0, 22.0),
    )
    walk_right_hip = rotation_accessor(
        "WALK_RIGHT_HIP", (1.0, 0.0, 0.0), (-22.0, 0.0, 22.0, 0.0, -22.0),
    )
    walk_left_knee = rotation_accessor(
        "WALK_LEFT_KNEE", (1.0, 0.0, 0.0), (0.0, 26.0, 0.0, 7.0, 0.0),
    )
    walk_right_knee = rotation_accessor(
        "WALK_RIGHT_KNEE", (1.0, 0.0, 0.0), (0.0, 7.0, 0.0, 26.0, 0.0),
    )
    run_left_hip = rotation_accessor(
        "RUN_LEFT_HIP", (1.0, 0.0, 0.0), (38.0, 0.0, -38.0, 0.0, 38.0),
    )
    run_right_hip = rotation_accessor(
        "RUN_RIGHT_HIP", (1.0, 0.0, 0.0), (-38.0, 0.0, 38.0, 0.0, -38.0),
    )
    run_left_knee = rotation_accessor(
        "RUN_LEFT_KNEE", (1.0, 0.0, 0.0), (8.0, 48.0, 8.0, 18.0, 8.0),
    )
    run_right_knee = rotation_accessor(
        "RUN_RIGHT_KNEE", (1.0, 0.0, 0.0), (8.0, 18.0, 8.0, 48.0, 8.0),
    )
    wave_shoulder = rotation_accessor(
        "WAVE_LEFT_SHOULDER", (0.0, 0.0, 1.0), (0.0, 50.0, 68.0, 50.0, 0.0),
    )
    wave_elbow = rotation_accessor(
        "WAVE_LEFT_ELBOW", (1.0, 0.0, 0.0), (0.0, -28.0, -58.0, -28.0, 0.0),
    )
    breathe_weight_accessor = accessor(
        _float_payload((0.0, 0.0, 1.0, 0.0, 0.0, 0.0)),
        5126, 6, "SCALAR", "MORPH_BREATHE_WEIGHT",
    )
    focus_weight_accessor = accessor(
        _float_payload((0.0, 0.0, 0.0, 1.0, 0.0, 0.0)),
        5126, 6, "SCALAR", "MORPH_FOCUS_WEIGHT",
    )

    base_color_png, surface_png, normal_png = _material_textures(palette, seed)
    base_color_view = builder.add(base_color_png)
    surface_view = builder.add(surface_png)
    normal_view = builder.add(normal_png)

    binary = bytes(builder.payload)
    def animation(name, time_index, tracks):
        return {
            "channels": [
                {"sampler": index, "target": {"node": node, "path": target_path}}
                for index, (node, target_path, _output) in enumerate(tracks)
            ],
            "name": name,
            "samplers": [
                {"input": time_index, "interpolation": "LINEAR", "output": output}
                for _node, _target_path, output in tracks
            ],
        }

    animations = [
        animation("Idle", short_time_accessor, (
            (2, "rotation", idle_rotation),
            (0, "translation", idle_translation),
        )),
        animation("Walk", cycle_time_accessor, (
            (11, "rotation", walk_left_hip),
            (14, "rotation", walk_right_hip),
            (12, "rotation", walk_left_knee),
            (15, "rotation", walk_right_knee),
        )),
        animation("Run", cycle_time_accessor, (
            (11, "rotation", run_left_hip),
            (14, "rotation", run_right_hip),
            (12, "rotation", run_left_knee),
            (15, "rotation", run_right_knee),
        )),
        animation("Wave", cycle_time_accessor, (
            (5, "rotation", wave_shoulder),
            (6, "rotation", wave_elbow),
        )),
        animation("Breathe", short_time_accessor, (
            (len(joint_specs), "weights", breathe_weight_accessor),
        )),
        animation("Focus", short_time_accessor, (
            (len(joint_specs), "weights", focus_weight_accessor),
        )),
    ]

    children = {index: [] for index in range(len(joint_specs))}
    for index, (_name, parent, _position) in enumerate(joint_specs):
        if parent is not None:
            children[parent].append(index)
    joint_nodes = []
    for index, (name, parent, world_position) in enumerate(joint_specs):
        parent_position = (0.0, 0.0, 0.0) if parent is None else joint_specs[parent][2]
        node = {
            "name": name,
            "translation": [
                round(value - parent_value, 7)
                for value, parent_value in zip(world_position, parent_position)
            ],
        }
        if children[index]:
            node["children"] = children[index]
        joint_nodes.append(node)
    mesh_node = len(joint_specs)
    humanoid_bones = {name: index for index, (name, _parent, _position) in enumerate(joint_specs)}
    document = {
        "accessors": accessors,
        "animations": animations,
        "asset": {
            "generator": "Trilobite stdlib rigged-model forge",
            "version": "2.0",
        },
        "bufferViews": builder.views,
        "buffers": [{"byteLength": len(binary)}],
        "extras": {
            "animationClips": [
                {"duration": 1.0, "index": index, "name": item["name"]}
                for index, item in enumerate(animations)
            ],
            "animationSequences": [
                {
                    "clips": ["Idle", "Walk", "Run", "Walk", "Idle"],
                    "loop": False,
                    "name": "LocomotionRamp",
                    "transitions": [0.2, 0.16, 0.16, 0.2],
                },
                {
                    "clips": ["Idle", "Breathe", "Focus"],
                    "loop": True,
                    "name": "AmbientCharacter",
                    "transitions": [0.25, 0.25],
                },
            ],
            "brief": _clean(brief),
            "humanoidBones": humanoid_bones,
            "seed": seed,
            "theme": _clean(theme, 32),
            "units": "meters",
            "upAxis": "Y",
        },
        "images": [
            {
                "bufferView": base_color_view,
                "mimeType": "image/png",
                "name": "%s Base Color" % _clean(title, 64),
            },
            {
                "bufferView": surface_view,
                "mimeType": "image/png",
                "name": "%s Surface" % _clean(title, 64),
            },
            {
                "bufferView": normal_view,
                "mimeType": "image/png",
                "name": "%s Normal" % _clean(title, 64),
            },
        ],
        "materials": [{
            "doubleSided": False,
            "emissiveFactor": [
                round(channel / 255.0 * 0.08, 6) for channel in palette[2]
            ],
            "name": "%s %s shell" % (_clean(title, 64), _clean(theme, 32)),
            "normalTexture": {"index": 2, "scale": 0.7},
            "occlusionTexture": {"index": 1, "strength": 0.85},
            "pbrMetallicRoughness": {
                "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                "baseColorTexture": {"index": 0},
                "metallicFactor": 0.72,
                "metallicRoughnessTexture": {"index": 1},
                "roughnessFactor": 0.92,
            },
        }],
        "meshes": [{
            "name": "%s skinned mesh" % _clean(title, 64),
            "primitives": [{
                "attributes": {
                    "JOINTS_0": joint_accessor,
                    "NORMAL": normal_accessor,
                    "POSITION": position_accessor,
                    "TANGENT": tangent_accessor,
                    "TEXCOORD_0": texcoord_accessor,
                    "WEIGHTS_0": weight_accessor,
                },
                "indices": index_accessor,
                "material": 0,
                "mode": 4,
                "targets": morph_targets,
            }],
            "extras": {"targetNames": ["Breathe", "Focus"]},
        }],
        "nodes": joint_nodes + [{
            "mesh": 0,
            "name": "%sModel" % _clean(title, 64),
            "skin": 0,
            "weights": [0.0, 0.0],
        }],
        "scene": 0,
        "scenes": [{"name": "%s Scene" % _clean(title, 64), "nodes": [0, mesh_node]}],
        "samplers": [{
            "magFilter": 9729,
            "minFilter": 9987,
            "wrapS": 10497,
            "wrapT": 10497,
        }],
        "skins": [{
            "inverseBindMatrices": bind_accessor,
            "joints": list(range(len(joint_specs))),
            "name": "%s Rig" % _clean(title, 64),
            "skeleton": 0,
        }],
        "textures": [
            {"name": "%s Base Color" % _clean(title, 64), "sampler": 0, "source": 0},
            {"name": "%s Surface" % _clean(title, 64), "sampler": 0, "source": 1},
            {"name": "%s Normal" % _clean(title, 64), "sampler": 0, "source": 2},
        ],
    }
    json_payload = json.dumps(
        document, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    json_payload += b" " * (-len(json_payload) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    total_length = 12 + 8 + len(json_payload) + 8 + len(binary)
    glb = (
        struct.pack("<4sII", GLB_MAGIC, GLB_VERSION, total_length)
        + struct.pack("<II", len(json_payload), JSON_CHUNK)
        + json_payload
        + struct.pack("<II", len(binary), BIN_CHUNK)
        + binary
    )
    _atomic_write_bytes(path, glb)
    return {
        "animation_sequences": 2,
        "animations": len(animations),
        "bytes": len(glb),
        "images": 3,
        "joints": len(joint_specs),
        "materials": 1,
        "morph_targets": len(morph_targets),
        "textures": 3,
        "triangles": len(indices) // 3,
        "vertices": len(positions),
    }
