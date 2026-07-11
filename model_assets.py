"""Deterministic, dependency-free textured and rigged glTF 2.0 generation."""

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


def write_rigged_glb(path, palette, theme="arcane", seed=1337, title="Trilobite", brief=""):
    """Write a textured GLB containing geometry, a two-joint skin, and animation."""
    seed = int(seed)
    variation = ((seed & 255) / 255.0 - 0.5) * 0.16
    joint_height = round(0.9 + variation * 0.25, 6)
    shell_width = round(0.82 + variation, 6)

    positions = []
    normals = []
    tangents = []
    texcoords = []
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
        positions, normals, tangents, texcoords, joints, weights, indices,
        (-0.55, 0.0, -0.38, 0.55, joint_height, 0.38), root_skin,
    )
    _append_box(
        positions, normals, tangents, texcoords, joints, weights, indices,
        (-shell_width, joint_height - 0.18, -0.5, shell_width, 1.7, 0.5), upper_skin,
    )
    _append_box(
        positions, normals, tangents, texcoords, joints, weights, indices,
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
    tangent_values = _flatten(tangents)
    texcoord_values = _flatten(texcoords)
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

    base_color_png, surface_png, normal_png = _material_textures(palette, seed)
    base_color_view = builder.add(base_color_png)
    surface_view = builder.add(surface_png)
    normal_view = builder.add(normal_png)

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
            }],
        }],
        "nodes": [
            {"children": [1], "name": "RootJoint"},
            {"name": "ShellJoint", "translation": [0.0, joint_height, 0.0]},
            {"mesh": 0, "name": "%sModel" % _clean(title, 64), "skin": 0},
        ],
        "scene": 0,
        "scenes": [{"name": "%s Scene" % _clean(title, 64), "nodes": [0, 2]}],
        "samplers": [{
            "magFilter": 9729,
            "minFilter": 9987,
            "wrapS": 10497,
            "wrapT": 10497,
        }],
        "skins": [{
            "inverseBindMatrices": bind_accessor,
            "joints": [0, 1],
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
        "animations": 1,
        "bytes": len(glb),
        "images": 3,
        "joints": 2,
        "materials": 1,
        "textures": 3,
        "triangles": len(indices) // 3,
        "vertices": len(positions),
    }
