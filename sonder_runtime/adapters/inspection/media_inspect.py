"""Guarded, bounded, stdlib-only inspection for audio, video, MIDI, and 3D assets.

Supports:
- Audio: ``.wav`` (PCM parameters, duration, bitrate, peak/RMS loudness, clipping/silence).
- MIDI: ``.mid`` / ``.midi`` (format, track count, PPQ, tempo/note/event counts).
- Video: ``.avi`` (RIFF/avih dimensions, FPS, total frames, duration, stream types).
- 3D models: ``.glb`` / ``.gltf`` (glTF version, scenes, nodes, meshes, skins, joints,
  materials, textures, animations, vertex/triangle counts, bounding box), ``.obj``
  (vertices/normals/UVs/faces/groups/materials + bbox), and ``.stl`` (binary & ASCII
  triangle count + bbox).

Security invariants:
- Enforces ``file_ops.require_read_access``, reparse-point refusal, sensitive-directory
  refusal, and bounded read/scan ceilings. Pure stdlib, zero subprocesses.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any, Sequence
import wave

import sonder_runtime.adapters.filesystem.file_ops as file_ops


SUPPORTED_FORMATS = frozenset({"wav", "midi", "avi", "glb", "gltf", "obj", "stl"})
_SUFFIX_MAP = {
    ".wav": "wav",
    ".mid": "midi",
    ".midi": "midi",
    ".avi": "avi",
    ".glb": "glb",
    ".gltf": "gltf",
    ".obj": "obj",
    ".stl": "stl",
}

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_JSON_CHUNK_BYTES = 8 * 1024 * 1024
MAX_TEXT_SCAN_BYTES = 16 * 1024 * 1024
MAX_WAV_ANALYSIS_FRAMES = 500_000


class MediaInspectError(ValueError):
    """Stable rejection from bounded media/3D inspection."""


def _resolve_target(
    path: str,
    *,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> Path:
    requested = file_ops._requested_path(str(path or ""))
    if file_ops._is_reparse_point(requested):
        raise MediaInspectError("media target must not be a symlink or junction")
    try:
        target = file_ops.require_read_access(
            str(path or ""),
            extra_roots=extra_roots,
            bypass=bypass,
            developer_authorized=developer_authorized,
        )
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        raise MediaInspectError("media path rejected: %s" % exc) from exc
    if not target.exists() or not target.is_file():
        raise MediaInspectError("media target must be an existing regular file")
    for root in file_ops.allowed_roots(extra_roots if bypass else ""):
        resolved_root = file_ops._resolve_best_effort(root)
        if target == resolved_root or file_ops._is_inside(target, resolved_root):
            relative = target.relative_to(resolved_root)
            if any(
                part.lower() in file_ops.SENSITIVE_READ_DIRECTORIES
                for part in relative.parts
            ):
                raise MediaInspectError(
                    "media path is secret or repository control state"
                )
            break
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise MediaInspectError(
            "media file exceeds maximum size ceiling (%d > %d bytes)" % (size, MAX_FILE_BYTES)
        )
    return target


def _file_sha256(target: Path) -> str:
    h = hashlib.sha256()
    with target.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _inspect_wav(target: Path) -> dict[str, Any]:
    try:
        with wave.open(str(target), "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            frame_count = wf.getnframes()
            comp_type = wf.getcomptype()
            frames_to_read = min(frame_count, MAX_WAV_ANALYSIS_FRAMES)
            raw = wf.readframes(frames_to_read)
    except (OSError, wave.Error, EOFError) as exc:
        raise MediaInspectError("invalid WAV file: %s" % exc) from exc

    duration = round(frame_count / sample_rate, 4) if sample_rate > 0 else 0.0
    bitrate_kbps = round((sample_rate * channels * sample_width * 8) / 1000.0, 1)
    peak_norm = None
    rms_norm = None
    clipped = False
    silent = False

    if raw and sample_width in (1, 2, 4):
        samples: Sequence[int]
        if sample_width == 1:
            samples = [b - 128 for b in raw]
            max_amp = 128.0
        elif sample_width == 2:
            count = len(raw) // 2
            samples = struct.unpack("<" + "h" * count, raw[: count * 2])
            max_amp = 32768.0
        else:
            count = len(raw) // 4
            samples = struct.unpack("<" + "i" * count, raw[: count * 4])
            max_amp = 2147483648.0
        if samples:
            peak = max(abs(s) for s in samples)
            peak_norm = round(peak / max_amp, 4)
            rms = math.sqrt(sum(s * s for s in samples) / len(samples))
            rms_norm = round(rms / max_amp, 4)
            clipped = peak_norm >= 0.999
            silent = peak_norm < 0.0005

    return {
        "channels": channels,
        "sample_rate_hz": sample_rate,
        "bit_depth": sample_width * 8,
        "frame_count": frame_count,
        "duration_seconds": duration,
        "bitrate_kbps": bitrate_kbps,
        "compression": comp_type,
        "peak_normalized": peak_norm,
        "rms_normalized": rms_norm,
        "clipped": clipped,
        "silent": silent,
        "analysis_truncated": frame_count > MAX_WAV_ANALYSIS_FRAMES,
    }


def _inspect_midi(target: Path) -> dict[str, Any]:
    raw = target.read_bytes()
    if len(raw) < 14 or raw[:4] != b"MThd":
        raise MediaInspectError("invalid MIDI header (missing MThd)")
    hdr_len = struct.unpack(">I", raw[4:8])[0]
    if hdr_len < 6 or len(raw) < 8 + hdr_len:
        raise MediaInspectError("truncated MIDI header")
    midi_format, num_tracks, division = struct.unpack(">HHH", raw[8:14])
    pos = 8 + hdr_len
    tracks_parsed = 0
    note_on_count = 0
    tempo_events = 0
    while pos + 8 <= len(raw) and tracks_parsed < 256:
        chunk_id = raw[pos : pos + 4]
        chunk_len = struct.unpack(">I", raw[pos + 4 : pos + 8])[0]
        pos += 8
        if chunk_len > len(raw) - pos:
            break
        chunk = raw[pos : pos + chunk_len]
        pos += chunk_len
        if chunk_id != b"MTrk":
            continue
        tracks_parsed += 1
        i = 0
        running_status = 0
        while i < len(chunk):
            # Variable-length delta time
            while i < len(chunk) and (chunk[i] & 0x80):
                i += 1
            i += 1
            if i >= len(chunk):
                break
            byte = chunk[i]
            if byte & 0x80:
                status = byte
                i += 1
                if status < 0xF0:
                    running_status = status
            else:
                status = running_status
            if status == 0xFF:
                if i + 1 >= len(chunk):
                    break
                meta_type = chunk[i]
                i += 1
                length = 0
                while i < len(chunk):
                    b = chunk[i]
                    i += 1
                    length = (length << 7) | (b & 0x7F)
                    if not (b & 0x80):
                        break
                if meta_type == 0x51:
                    tempo_events += 1
                i += length
            elif status in (0xF0, 0xF7):
                length = 0
                while i < len(chunk):
                    b = chunk[i]
                    i += 1
                    length = (length << 7) | (b & 0x7F)
                    if not (b & 0x80):
                        break
                i += length
            else:
                ev_type = status & 0xF0
                if ev_type in (0xC0, 0xD0):
                    i += 1
                else:
                    if ev_type == 0x90 and i + 1 < len(chunk) and chunk[i + 1] > 0:
                        note_on_count += 1
                    i += 2

    return {
        "midi_format": midi_format,
        "declared_tracks": num_tracks,
        "parsed_tracks": tracks_parsed,
        "ticks_per_quarter": division if not (division & 0x8000) else None,
        "note_on_count": note_on_count,
        "tempo_events": tempo_events,
    }


def _inspect_avi(target: Path) -> dict[str, Any]:
    with target.open("rb") as handle:
        head = handle.read(65536)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"AVI ":
        raise MediaInspectError("invalid AVI container (missing RIFF/AVI header)")
    riff_size = struct.unpack("<I", head[4:8])[0]
    avih_idx = head.find(b"avih")
    if avih_idx < 0 or avih_idx + 8 + 56 > len(head):
        raise MediaInspectError("AVI container is missing avih main header")
    fields = struct.unpack("<14I", head[avih_idx + 8 : avih_idx + 64])
    microsec_per_frame = fields[0]
    total_frames = fields[4]
    streams = fields[6]
    width = fields[8]
    height = fields[9]
    fps = round(1_000_000.0 / microsec_per_frame, 3) if microsec_per_frame > 0 else 0.0
    duration = round((total_frames * microsec_per_frame) / 1_000_000.0, 3) if microsec_per_frame > 0 else 0.0
    stream_types: list[str] = []
    pos = 0
    while True:
        idx = head.find(b"strh", pos)
        if idx < 0 or idx + 12 > len(head):
            break
        fcc = head[idx + 8 : idx + 12].decode("ascii", errors="replace")
        stream_types.append(fcc)
        pos = idx + 12
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "total_frames": total_frames,
        "duration_seconds": duration,
        "stream_count": streams,
        "stream_types": stream_types,
        "declared_riff_bytes": riff_size + 8,
    }


def _inspect_gltf_json(doc: dict[str, Any], *, binary_chunk_bytes: int = 0) -> dict[str, Any]:
    asset = doc.get("asset") or {}
    meshes = doc.get("meshes") or []
    nodes = doc.get("nodes") or []
    skins = doc.get("skins") or []
    materials = doc.get("materials") or []
    textures = doc.get("textures") or []
    images = doc.get("images") or []
    animations = doc.get("animations") or []
    accessors = doc.get("accessors") or []

    total_vertices = 0
    total_triangles = 0
    bbox_min: list[float] | None = None
    bbox_max: list[float] | None = None

    for mesh in meshes:
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            pos_idx = attrs.get("POSITION")
            if isinstance(pos_idx, int) and 0 <= pos_idx < len(accessors):
                acc = accessors[pos_idx]
                cnt = int(acc.get("count") or 0)
                total_vertices += cnt
                amin = acc.get("min")
                amax = acc.get("max")
                if isinstance(amin, list) and len(amin) == 3 and all(isinstance(v, (int, float)) for v in amin):
                    bbox_min = list(amin) if bbox_min is None else [min(bbox_min[i], amin[i]) for i in range(3)]
                if isinstance(amax, list) and len(amax) == 3 and all(isinstance(v, (int, float)) for v in amax):
                    bbox_max = list(amax) if bbox_max is None else [max(bbox_max[i], amax[i]) for i in range(3)]
            idx_acc_i = prim.get("indices")
            mode = prim.get("mode", 4)
            if mode == 4:
                if isinstance(idx_acc_i, int) and 0 <= idx_acc_i < len(accessors):
                    total_triangles += int(accessors[idx_acc_i].get("count") or 0) // 3
                elif isinstance(pos_idx, int) and 0 <= pos_idx < len(accessors):
                    total_triangles += int(accessors[pos_idx].get("count") or 0) // 3

    joint_count = sum(len(s.get("joints") or []) for s in skins)
    return {
        "gltf_version": str(asset.get("version", "")),
        "generator": str(asset.get("generator", "")),
        "mesh_count": len(meshes),
        "node_count": len(nodes),
        "skin_count": len(skins),
        "joint_count": joint_count,
        "material_count": len(materials),
        "texture_count": len(textures),
        "image_count": len(images),
        "animation_count": len(animations),
        "animation_names": [str(a.get("name", "")) for a in animations[:20]],
        "vertex_count": total_vertices,
        "triangle_count": total_triangles,
        "bbox_min": [round(v, 4) for v in bbox_min] if bbox_min else None,
        "bbox_max": [round(v, 4) for v in bbox_max] if bbox_max else None,
        "binary_chunk_bytes": binary_chunk_bytes,
    }


def _inspect_glb(target: Path) -> dict[str, Any]:
    with target.open("rb") as handle:
        header = handle.read(12)
        if len(header) < 12 or header[:4] != b"glTF":
            raise MediaInspectError("invalid GLB header (missing glTF magic)")
        version, total_len = struct.unpack("<II", header[4:12])
        chunk_hdr = handle.read(8)
        if len(chunk_hdr) < 8:
            raise MediaInspectError("truncated GLB JSON chunk header")
        json_len, chunk_type = struct.unpack("<II", chunk_hdr)
        if chunk_type != 0x4E4F534A:
            raise MediaInspectError("first GLB chunk is not JSON")
        if json_len > MAX_JSON_CHUNK_BYTES:
            raise MediaInspectError("GLB JSON chunk exceeds ceiling")
        json_raw = handle.read(json_len)
        bin_hdr = handle.read(8)
        bin_len = 0
        if len(bin_hdr) == 8:
            b_len, b_type = struct.unpack("<II", bin_hdr)
            if b_type == 0x004E4942:
                bin_len = b_len
    try:
        doc = json.loads(json_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaInspectError("malformed GLB JSON chunk: %s" % exc) from exc
    result = _inspect_gltf_json(doc, binary_chunk_bytes=bin_len)
    result["container_version"] = version
    result["declared_length"] = total_len
    return result


def _inspect_gltf(target: Path) -> dict[str, Any]:
    if target.stat().st_size > MAX_JSON_CHUNK_BYTES:
        raise MediaInspectError("glTF JSON file exceeds size ceiling")
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaInspectError("malformed glTF JSON: %s" % exc) from exc
    return _inspect_gltf_json(doc)


def _inspect_obj(target: Path) -> dict[str, Any]:
    if target.stat().st_size > MAX_TEXT_SCAN_BYTES:
        raise MediaInspectError("OBJ file exceeds scan ceiling")
    vertices = normals = uvs = faces = triangles = 0
    objects: list[str] = []
    groups: list[str] = []
    materials: set[str] = set()
    bmin: list[float] | None = None
    bmax: list[float] | None = None

    with target.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("v "):
                vertices += 1
                parts = s.split()
                if len(parts) >= 4:
                    try:
                        coords = [float(parts[1]), float(parts[2]), float(parts[3])]
                    except ValueError:
                        continue
                    if all(math.isfinite(c) for c in coords):
                        bmin = list(coords) if bmin is None else [min(bmin[i], coords[i]) for i in range(3)]
                        bmax = list(coords) if bmax is None else [max(bmax[i], coords[i]) for i in range(3)]
            elif s.startswith("vn "):
                normals += 1
            elif s.startswith("vt "):
                uvs += 1
            elif s.startswith("f "):
                faces += 1
                n_verts = len(s.split()) - 1
                if n_verts >= 3:
                    triangles += n_verts - 2
            elif s.startswith("o ") and len(objects) < 50:
                objects.append(s[2:].strip())
            elif s.startswith("g ") and len(groups) < 50:
                groups.append(s[2:].strip())
            elif s.startswith("usemtl "):
                materials.add(s[7:].strip())

    return {
        "vertex_count": vertices,
        "normal_count": normals,
        "uv_count": uvs,
        "face_count": faces,
        "triangle_count": triangles,
        "objects": objects,
        "groups": groups,
        "materials": sorted(materials)[:50],
        "bbox_min": [round(v, 4) for v in bmin] if bmin else None,
        "bbox_max": [round(v, 4) for v in bmax] if bmax else None,
    }


def _inspect_stl(target: Path) -> dict[str, Any]:
    size = target.stat().st_size
    with target.open("rb") as handle:
        header = handle.read(84)
    if len(header) >= 84:
        tri_count = struct.unpack("<I", header[80:84])[0]
        if 84 + tri_count * 50 == size:
            bmin: list[float] | None = None
            bmax: list[float] | None = None
            sample_cap = min(tri_count, 200_000)
            with target.open("rb") as handle:
                handle.seek(84)
                for _ in range(sample_cap):
                    rec = handle.read(50)
                    if len(rec) < 50:
                        break
                    vals = struct.unpack("<12f", rec[:48])
                    for offset in (3, 6, 9):
                        pt = vals[offset : offset + 3]
                        if all(math.isfinite(c) for c in pt):
                            bmin = list(pt) if bmin is None else [min(bmin[i], pt[i]) for i in range(3)]
                            bmax = list(pt) if bmax is None else [max(bmax[i], pt[i]) for i in range(3)]
            return {
                "encoding": "binary",
                "header_text": header[:80].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip(),
                "triangle_count": tri_count,
                "bbox_min": [round(v, 4) for v in bmin] if bmin else None,
                "bbox_max": [round(v, 4) for v in bmax] if bmax else None,
            }
    if size > MAX_TEXT_SCAN_BYTES:
        raise MediaInspectError("ASCII STL exceeds scan ceiling")
    tri_count = 0
    bmin = None
    bmax = None
    with target.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            s = line.strip().lower()
            if s.startswith("facet normal"):
                tri_count += 1
            elif s.startswith("vertex "):
                parts = s.split()
                if len(parts) >= 4:
                    try:
                        vertex_pt = [float(parts[1]), float(parts[2]), float(parts[3])]
                    except ValueError:
                        continue
                    if all(math.isfinite(c) for c in vertex_pt):
                        bmin = list(vertex_pt) if bmin is None else [min(bmin[i], vertex_pt[i]) for i in range(3)]
                        bmax = list(vertex_pt) if bmax is None else [max(bmax[i], vertex_pt[i]) for i in range(3)]
    return {
        "encoding": "ascii",
        "triangle_count": tri_count,
        "bbox_min": [round(v, 4) for v in bmin] if bmin else None,
        "bbox_max": [round(v, 4) for v in bmax] if bmax else None,
    }


def inspect_media(
    path: str,
    *,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict[str, Any]:
    """Inspect a guarded audio, MIDI, video, or 3D model file."""
    target = _resolve_target(
        path,
        extra_roots=extra_roots,
        bypass=bypass,
        developer_authorized=developer_authorized,
    )
    fmt = _SUFFIX_MAP.get(target.suffix.lower())
    if not fmt:
        raise MediaInspectError(
            "unsupported media/3D extension %r (supported: %s)"
            % (target.suffix, ", ".join(sorted(_SUFFIX_MAP)))
        )
    if fmt == "wav":
        details = _inspect_wav(target)
    elif fmt == "midi":
        details = _inspect_midi(target)
    elif fmt == "avi":
        details = _inspect_avi(target)
    elif fmt == "glb":
        details = _inspect_glb(target)
    elif fmt == "gltf":
        details = _inspect_gltf(target)
    elif fmt == "obj":
        details = _inspect_obj(target)
    else:
        details = _inspect_stl(target)

    return {
        "ok": True,
        "path": str(target),
        "format": fmt,
        "bytes": target.stat().st_size,
        "sha256": _file_sha256(target),
        **details,
    }


def format_result(data: dict[str, Any]) -> str:
    lines = [
        f"media inspection ({data.get('format', 'unknown')}): {data.get('path', '')}",
        f"  bytes: {data.get('bytes', 0)}  sha256: {data.get('sha256', '')}",
    ]
    skip = {"ok", "path", "format", "bytes", "sha256"}
    for key in sorted(data):
        if key in skip or data[key] in (None, "", []):
            continue
        lines.append(f"  {key}: {data[key]}")
    return "\n".join(lines)


__all__ = [
    "MediaInspectError",
    "SUPPORTED_FORMATS",
    "format_result",
    "inspect_media",
]
