"""Deterministic, dependency-free rigged glTF 2.0 model generation."""

from __future__ import annotations

import json
import math
import os
import struct
import tempfile
import time


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


def _append_box(positions, normals, joints, weights, indices, bounds, skin):
    x0, y0, z0, x1, y1, z1 = bounds
    faces = (
        ((1.0, 0.0, 0.0), ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))),
        ((-1.0, 0.0, 0.0), ((x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (x0, y0, z0))),
        ((0.0, 1.0, 0.0), ((x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0))),
        ((0.0, -1.0, 0.0), ((x0, y0, z1), (x0, y0, z0), (x1, y0, z0), (x1, y0, z1))),
        ((0.0, 0.0, 1.0), ((x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (x0, y0, z1))),
        ((0.0, 0.0, -1.0), ((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0))),
    )
    for normal, corners in faces:
        base = len(positions)
        for position in corners:
            positions.append(position)
            normals.append(normal)
            joint_row, weight_row = skin(position[1])
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


def _inverse_translation_y(value):
    return (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, -value, 0.0, 1.0,
    )


def _factor(rgb):
    return [round(channel / 255.0, 6) for channel in rgb] + [1.0]


def write_rigged_glb(path, palette, theme="arcane", seed=1337, title="Trilobite", brief=""):
    """Write a compact GLB containing geometry, a two-joint skin, and animation."""
    seed = int(seed)
    variation = ((seed & 255) / 255.0 - 0.5) * 0.16
    joint_height = round(0.9 + variation * 0.25, 6)
    shell_width = round(0.82 + variation, 6)

    positions = []
    normals = []
    joints = []
    weights = []
    indices = []

    def root_skin(_y):
        return (0, 0, 0, 0), (1.0, 0.0, 0.0, 0.0)

    def upper_skin(y):
        blend = max(0.0, min(1.0, (y - joint_height + 0.22) / 0.44))
        blend = round(0.3 + 0.7 * blend, 6)
        return (0, 1, 0, 0), (round(1.0 - blend, 6), blend, 0.0, 0.0)

    _append_box(
        positions, normals, joints, weights, indices,
        (-0.55, 0.0, -0.38, 0.55, joint_height, 0.38), root_skin,
    )
    _append_box(
        positions, normals, joints, weights, indices,
        (-shell_width, joint_height - 0.18, -0.5, shell_width, 1.7, 0.5), upper_skin,
    )
    _append_box(
        positions, normals, joints, weights, indices,
        (-0.42, 1.62, -0.34, 0.42, 2.18 + variation, 0.34), upper_skin,
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
    joint_values = _flatten(joints)
    weight_values = _flatten(weights)
    mins = [min(row[axis] for row in positions) for axis in range(3)]
    maxs = [max(row[axis] for row in positions) for axis in range(3)]
    position_accessor = accessor(
        _float_payload(position_values), 5126, len(positions), "VEC3", "POSITION",
        34962, mins, maxs,
    )
    normal_accessor = accessor(
        _float_payload(normal_values), 5126, len(normals), "VEC3", "NORMAL", 34962,
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
    matrices = _identity_matrix() + _inverse_translation_y(joint_height)
    bind_accessor = accessor(
        _float_payload(matrices), 5126, 2, "MAT4", "INVERSE_BIND_MATRICES",
    )
    times = (0.0, 0.5, 1.0)
    time_accessor = accessor(
        _float_payload(times), 5126, len(times), "SCALAR", "ANIMATION_TIME",
        minimum=[0.0], maximum=[1.0],
    )
    angle = math.radians(18.0 + abs(variation) * 80.0)
    rotations = (
        (0.0, 0.0, 0.0, 1.0),
        (0.0, 0.0, math.sin(angle / 2.0), math.cos(angle / 2.0)),
        (0.0, 0.0, 0.0, 1.0),
    )
    rotation_accessor = accessor(
        _float_payload(_flatten(rotations)), 5126, len(rotations), "VEC4", "SHELL_ROTATION",
    )

    binary = bytes(builder.payload)
    document = {
        "accessors": accessors,
        "animations": [{
            "channels": [{"sampler": 0, "target": {"node": 1, "path": "rotation"}}],
            "name": "ShellPulse",
            "samplers": [{
                "input": time_accessor,
                "interpolation": "LINEAR",
                "output": rotation_accessor,
            }],
        }],
        "asset": {
            "generator": "Trilobite stdlib rigged-model forge",
            "version": "2.0",
        },
        "bufferViews": builder.views,
        "buffers": [{"byteLength": len(binary)}],
        "extras": {
            "brief": _clean(brief),
            "seed": seed,
            "theme": _clean(theme, 32),
        },
        "materials": [{
            "doubleSided": False,
            "name": "%s %s shell" % (_clean(title, 64), _clean(theme, 32)),
            "pbrMetallicRoughness": {
                "baseColorFactor": _factor(palette[1]),
                "metallicFactor": 0.22,
                "roughnessFactor": 0.58,
            },
        }],
        "meshes": [{
            "name": "%s skinned mesh" % _clean(title, 64),
            "primitives": [{
                "attributes": {
                    "JOINTS_0": joint_accessor,
                    "NORMAL": normal_accessor,
                    "POSITION": position_accessor,
                    "WEIGHTS_0": weight_accessor,
                },
                "indices": index_accessor,
                "material": 0,
                "mode": 4,
            }],
        }],
        "nodes": [
            {"children": [1], "name": "RootJoint"},
            {"name": "ShellJoint", "translation": [0.0, joint_height, 0.0]},
            {"mesh": 0, "name": "%sModel" % _clean(title, 64), "skin": 0},
        ],
        "scene": 0,
        "scenes": [{"name": "%s Scene" % _clean(title, 64), "nodes": [0, 2]}],
        "skins": [{
            "inverseBindMatrices": bind_accessor,
            "joints": [0, 1],
            "name": "%s Rig" % _clean(title, 64),
            "skeleton": 0,
        }],
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
        "animations": 1,
        "bytes": len(glb),
        "joints": 2,
        "triangles": len(indices) // 3,
        "vertices": len(positions),
    }
