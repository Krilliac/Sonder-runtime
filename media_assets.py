"""Deterministic, dependency-free editable media and timeline generation."""

from __future__ import annotations

import math
import os
import random
import re
import struct
from pathlib import Path


DEFAULT_FPS = 30


def _clean(value, limit=240):
    normalized = " ".join(str(value or "").strip().split())
    safe = "".join(
        character
        for character in normalized
        if ord(character) >= 0x20
        and not 0xD800 <= ord(character) <= 0xDFFF
        and ord(character) not in {0xFFFE, 0xFFFF}
    )
    return safe[:limit]


def _atomic_write_bytes(path, data):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        if temporary.exists():
            temporary.unlink()
        temporary.write_bytes(data)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_text(path, text):
    _atomic_write_bytes(path, text.encode("utf-8"))


def _mix(left, right, amount):
    amount = max(0.0, min(1.0, float(amount)))
    return tuple(
        max(0, min(255, int(round(a * (1.0 - amount) + b * amount))))
        for a, b in zip(left, right)
    )


def _gif_palette(theme_palette):
    base, accent, bright, surface = theme_palette
    return [
        tuple(base),
        _mix(base, surface, 0.55),
        tuple(surface),
        _mix(accent, base, 0.28),
        tuple(accent),
        _mix(accent, bright, 0.45),
        tuple(bright),
        (245, 248, 255),
    ]


def _pack_lsb_codes(codes, code_size):
    output = bytearray()
    accumulator = 0
    bits = 0
    for code in codes:
        accumulator |= int(code) << bits
        bits += code_size
        while bits >= 8:
            output.append(accumulator & 0xFF)
            accumulator >>= 8
            bits -= 8
    if bits:
        output.append(accumulator & 0xFF)
    return bytes(output)


def _gif_lzw_literal_stream(pixels, minimum_code_size=3):
    """Encode pixels with frequent clear codes for a tiny, auditable encoder."""
    clear = 1 << minimum_code_size
    end = clear + 1
    codes = []
    for pixel in pixels:
        codes.extend((clear, int(pixel)))
    codes.append(end)
    return _pack_lsb_codes(codes, minimum_code_size + 1)


def _gif_subblocks(data):
    output = bytearray()
    for offset in range(0, len(data), 255):
        block = data[offset : offset + 255]
        output.append(len(block))
        output.extend(block)
    output.append(0)
    return bytes(output)


def _draw_circle(pixels, width, height, cx, cy, radius, color):
    radius_squared = radius * radius
    for y in range(max(0, cy - radius), min(height, cy + radius + 1)):
        for x in range(max(0, cx - radius), min(width, cx + radius + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius_squared:
                pixels[y * width + x] = color


def _draw_rect(pixels, width, height, x, y, rect_width, rect_height, color):
    for py in range(max(0, y), min(height, y + rect_height)):
        start = py * width + max(0, x)
        end = py * width + min(width, x + rect_width)
        if end > start:
            pixels[start:end] = bytes([color]) * (end - start)


def _animation_frame(width, height, frame, frame_count, stars):
    pixels = bytearray(width * height)
    horizon = int(height * 0.72)
    for y in range(height):
        row_color = 0 if y < horizon else 1
        pixels[y * width : (y + 1) * width] = bytes([row_color]) * width
    for index, (x, y) in enumerate(stars):
        pixels[y * width + x] = 6 if (index + frame) % 3 == 0 else 5
    for x in range(0, width, 8):
        pixels[(horizon + (x // 8) % 2) * width + x] = 3
    progress = frame / float(max(1, frame_count - 1))
    cx = 12 + int(progress * (width - 24))
    cy = int(height * 0.47 + math.sin(frame * math.pi / 2.0) * 4)
    for trail in range(1, 4):
        _draw_circle(pixels, width, height, cx - trail * 6, cy + trail, 3, 3)
    _draw_circle(pixels, width, height, cx, cy - 5, 5, 6)
    _draw_rect(pixels, width, height, cx - 6, cy, 12, 12, 4)
    _draw_rect(pixels, width, height, cx - 8, cy + 2, 2, 8, 5)
    _draw_rect(pixels, width, height, cx + 6, cy + 2, 2, 8, 5)
    _draw_rect(pixels, width, height, cx - 5 + frame % 2, cy + 12, 3, 5, 2)
    _draw_rect(pixels, width, height, cx + 2 - frame % 2, cy + 12, 3, 5, 2)
    if 0 <= cx - 2 < width and 0 <= cy - 6 < height:
        pixels[(cy - 6) * width + cx - 2] = 0
    if 0 <= cx + 2 < width and 0 <= cy - 6 < height:
        pixels[(cy - 6) * width + cx + 2] = 0
    return bytes(pixels)


def write_gif(path, theme_palette, seed=1337, width=96, height=54, frames=8):
    """Write a looping GIF89a animation with a deterministic indexed palette."""
    width = max(16, min(int(width), 320))
    height = max(16, min(int(height), 240))
    frames = max(2, min(int(frames), 60))
    palette = _gif_palette(theme_palette)
    rng = random.Random(int(seed))
    stars = [
        (rng.randrange(width), rng.randrange(2, max(3, int(height * 0.58))))
        for _ in range(max(8, width // 3))
    ]
    output = bytearray(b"GIF89a")
    output.extend(struct.pack("<HHBBB", width, height, 0xF2, 0, 0))
    for color in palette:
        output.extend(bytes(color))
    output.extend(b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00")
    for frame in range(frames):
        pixels = _animation_frame(width, height, frame, frames, stars)
        output.extend(b"\x21\xF9\x04\x04")
        output.extend(struct.pack("<H", 8))
        output.extend(b"\x00\x00")
        output.extend(b"\x2C")
        output.extend(struct.pack("<HHHHB", 0, 0, width, height, 0))
        output.append(3)
        output.extend(_gif_subblocks(_gif_lzw_literal_stream(pixels, 3)))
    output.append(0x3B)
    _atomic_write_bytes(path, bytes(output))


def _riff_chunk(kind, payload):
    if len(kind) != 4:
        raise ValueError("RIFF chunk identifiers must contain four bytes")
    padding = b"\x00" if len(payload) % 2 else b""
    return kind + struct.pack("<I", len(payload)) + payload + padding


def _riff_list(kind, payload):
    if len(kind) != 4:
        raise ValueError("RIFF list identifiers must contain four bytes")
    return _riff_chunk(b"LIST", kind + payload)


def _avi_frame(indexed_pixels, palette, width, height):
    row_bytes = width * 3
    stride = (row_bytes + 3) & ~3
    output = bytearray(stride * height)
    write_offset = 0
    for y in range(height - 1, -1, -1):
        row_offset = y * width
        for x in range(width):
            red, green, blue = palette[indexed_pixels[row_offset + x]]
            output[write_offset : write_offset + 3] = bytes((blue, green, red))
            write_offset += 3
        write_offset += stride - row_bytes
    return bytes(output)


def _avi_audio(frame_count, fps, theme):
    samples_per_frame = 1000
    sample_rate = fps * samples_per_frame
    total_samples = frame_count * samples_per_frame
    frequency = {"ember": 110.0, "verdant": 146.83, "frost": 220.0, "arcane": 174.61}.get(
        str(theme).lower(), 174.61
    )
    samples = bytearray()
    for index in range(total_samples):
        time = index / float(sample_rate)
        fade = min(1.0, index / float(max(1, sample_rate // 20)))
        fade *= min(1.0, (total_samples - index) / float(max(1, sample_rate // 12)))
        pulse = 0.72 + 0.28 * math.sin(2.0 * math.pi * 0.5 * time)
        value = math.sin(2.0 * math.pi * frequency * time)
        value += 0.22 * math.sin(2.0 * math.pi * frequency * 1.5 * time)
        sample = int(max(-1.0, min(1.0, value * pulse * fade * 0.22)) * 32767)
        samples.extend(struct.pack("<h", sample))
    return bytes(samples), sample_rate, samples_per_frame


def write_avi(
    path,
    theme_palette,
    theme="arcane",
    seed=1337,
    width=128,
    height=72,
    frames=48,
    fps=12,
):
    """Write an indexed-scene AVI with uncompressed 24-bit video and PCM audio."""
    width = max(16, min(int(width), 256))
    height = max(16, min(int(height), 144))
    frames = max(2, min(int(frames), 120))
    fps = max(1, min(int(fps), 60))
    palette = _gif_palette(theme_palette)
    rng = random.Random(int(seed))
    stars = [
        (rng.randrange(width), rng.randrange(2, max(3, int(height * 0.58))))
        for _ in range(max(8, width // 3))
    ]
    frame_payloads = [
        _avi_frame(
            _animation_frame(width, height, frame, frames, stars),
            palette,
            width,
            height,
        )
        for frame in range(frames)
    ]
    frame_size = len(frame_payloads[0])
    audio, sample_rate, samples_per_frame = _avi_audio(frames, fps, theme)
    block_align = 2
    audio_chunk_size = samples_per_frame * block_align
    average_audio_bytes = sample_rate * block_align

    main_header = struct.pack(
        "<14I",
        int(round(1_000_000 / float(fps))),
        frame_size * fps + average_audio_bytes,
        0,
        0x00000110,
        frames,
        0,
        2,
        max(frame_size, audio_chunk_size),
        width,
        height,
        0,
        0,
        0,
        0,
    )
    video_stream_header = struct.pack(
        "<4s4sIHHIIIIIIIIhhhh",
        b"vids",
        b"DIB ",
        0,
        0,
        0,
        0,
        1,
        fps,
        0,
        frames,
        frame_size,
        0xFFFFFFFF,
        0,
        0,
        0,
        width,
        height,
    )
    video_format = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height,
        1,
        24,
        0,
        frame_size,
        0,
        0,
        0,
        0,
    )
    audio_stream_header = struct.pack(
        "<4s4sIHHIIIIIIIIhhhh",
        b"auds",
        b"\x00\x00\x00\x00",
        0,
        0,
        0,
        0,
        block_align,
        average_audio_bytes,
        0,
        len(audio) // block_align,
        audio_chunk_size,
        0xFFFFFFFF,
        block_align,
        0,
        0,
        0,
        0,
    )
    audio_format = struct.pack(
        "<HHIIHH", 1, 1, sample_rate, average_audio_bytes, block_align, 16
    )
    video_stream = _riff_list(
        b"strl",
        _riff_chunk(b"strh", video_stream_header)
        + _riff_chunk(b"strf", video_format)
        + _riff_chunk(b"strn", b"Trilobite Video\x00"),
    )
    audio_stream = _riff_list(
        b"strl",
        _riff_chunk(b"strh", audio_stream_header)
        + _riff_chunk(b"strf", audio_format)
        + _riff_chunk(b"strn", b"Trilobite Audio\x00"),
    )
    header_list = _riff_list(
        b"hdrl", _riff_chunk(b"avih", main_header) + video_stream + audio_stream
    )

    movie_chunks = bytearray()
    index_entries = bytearray()
    for frame, frame_data in enumerate(frame_payloads):
        video_offset = 4 + len(movie_chunks)
        movie_chunks.extend(_riff_chunk(b"00db", frame_data))
        index_entries.extend(
            struct.pack("<4sIII", b"00db", 0x10, video_offset, len(frame_data))
        )
        audio_data = audio[
            frame * audio_chunk_size : (frame + 1) * audio_chunk_size
        ]
        audio_offset = 4 + len(movie_chunks)
        movie_chunks.extend(_riff_chunk(b"01wb", audio_data))
        index_entries.extend(
            struct.pack("<4sIII", b"01wb", 0, audio_offset, len(audio_data))
        )
    movie_list = _riff_list(b"movi", bytes(movie_chunks))
    payload = b"AVI " + header_list + movie_list + _riff_chunk(b"idx1", bytes(index_entries))
    _atomic_write_bytes(path, b"RIFF" + struct.pack("<I", len(payload)) + payload)


def _variable_length(value):
    value = max(0, min(int(value), 0x0FFFFFFF))
    buffer = [value & 0x7F]
    value >>= 7
    while value:
        buffer.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(buffer))


def _midi_meta(delta, kind, payload):
    return _variable_length(delta) + bytes((0xFF, kind)) + _variable_length(len(payload)) + payload


def write_midi(path, title, theme="arcane", seed=1337):
    """Write a format-0 Standard MIDI File with an editable melodic sequence."""
    rng = random.Random(int(seed))
    root = {"ember": 48, "verdant": 55, "frost": 60, "arcane": 57}.get(
        str(theme).lower(), 57
    )
    program = {"ember": 30, "verdant": 46, "frost": 88, "arcane": 81}.get(
        str(theme).lower(), 81
    )
    scale = (0, 3, 5, 7, 10, 12, 15, 17)
    name = _clean(title, 80).encode("utf-8") or b"Trilobite score"
    track = bytearray()
    track.extend(_midi_meta(0, 0x03, name))
    track.extend(_midi_meta(0, 0x51, bytes((0x07, 0xA1, 0x20))))
    track.extend(_variable_length(0) + bytes((0xC0, program)))
    for index in range(16):
        degree = (index * 2 + rng.randrange(0, 3)) % len(scale)
        note = min(96, root + scale[degree])
        velocity = 72 + rng.randrange(0, 28)
        lead_in = 0 if index == 0 else 120
        track.extend(_variable_length(lead_in) + bytes((0x90, note, velocity)))
        track.extend(_variable_length(360) + bytes((0x80, note, 0)))
    track.extend(_midi_meta(0, 0x2F, b""))
    payload = (
        b"MThd"
        + struct.pack(">IHHH", 6, 0, 1, 480)
        + b"MTrk"
        + struct.pack(">I", len(track))
        + bytes(track)
    )
    _atomic_write_bytes(path, payload)


def _caption_phrases(brief, count=6):
    subject = _clean(brief, 180) or "Generated media concept"
    labels = (
        "Opening",
        "Context",
        "Development",
        "Detail",
        "Resolution",
        "Closing",
    )
    return ["%s - %s" % (labels[index], subject) for index in range(count)]


def _timestamp(milliseconds, separator):
    milliseconds = max(0, int(milliseconds))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return "%02d:%02d:%02d%s%03d" % (
        hours,
        minutes,
        seconds,
        separator,
        millis,
    )


def write_srt(path, brief):
    """Write a deterministic UTF-8 SubRip caption track."""
    blocks = []
    for index, phrase in enumerate(_caption_phrases(brief), 1):
        start = (index - 1) * 2500
        end = start + 1800
        blocks.append(
            "%d\n%s --> %s\n%s"
            % (index, _timestamp(start, ","), _timestamp(end, ","), phrase)
        )
    _atomic_write_text(path, "\n\n".join(blocks) + "\n")


def write_vtt(path, brief):
    """Write a deterministic UTF-8 WebVTT caption track."""
    blocks = ["WEBVTT\n\nNOTE Generated locally by Trilobite"]
    for index, phrase in enumerate(_caption_phrases(brief), 1):
        start = (index - 1) * 2500
        end = start + 1800
        blocks.append(
            "cue-%02d\n%s --> %s\n%s"
            % (index, _timestamp(start, "."), _timestamp(end, "."), phrase)
        )
    _atomic_write_text(path, "\n\n".join(blocks) + "\n")


def _timecode(total_frames, fps=DEFAULT_FPS):
    total_frames = max(0, int(total_frames))
    hours, remainder = divmod(total_frames, fps * 3600)
    minutes, remainder = divmod(remainder, fps * 60)
    seconds, frames = divmod(remainder, fps)
    return "%02d:%02d:%02d:%02d" % (hours, minutes, seconds, frames)


def write_edl(path, title, brief, fps=DEFAULT_FPS, clip_name="preview.avi"):
    """Write a CMX 3600-style non-drop-frame editable video timeline."""
    fps = max(24, min(int(fps), 60))
    lines = ["TITLE: %s" % (_clean(title, 72) or "TRILOBITE TIMELINE"), "FCM: NON-DROP FRAME", ""]
    clip_name = Path(_clean(clip_name, 120)).name or "animation.gif"
    subject = re.sub(r"[^A-Za-z0-9_-]+", "_", _clean(brief, 40)).strip("_")
    record_start = fps * 3600
    duration = fps * 3
    for index in range(1, 7):
        source_in = 0
        source_out = duration
        record_in = record_start + (index - 1) * duration
        record_out = record_in + duration
        lines.append(
            "%03d  AX       V     C        %s %s %s %s"
            % (
                index,
                _timecode(source_in, fps),
                _timecode(source_out, fps),
                _timecode(record_in, fps),
                _timecode(record_out, fps),
            )
        )
        lines.append("* FROM CLIP NAME: %s" % clip_name)
        if subject:
            lines.append("* COMMENT: %s scene %02d" % (subject, index))
    _atomic_write_text(path, "\n".join(lines) + "\n")
