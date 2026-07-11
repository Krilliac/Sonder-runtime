import hashlib
import struct
from pathlib import Path

import pytest

import artifact_grounding
import media_assets


PALETTE = ((17, 15, 35), (116, 91, 218), (87, 218, 207), (49, 38, 91))


CASES = [
    (
        "preview.avi",
        lambda path: media_assets.write_avi(path, PALETTE, "arcane", 42),
        {"min_frames": 48, "min_duration_ms": 4000, "require_audio": True},
        b"RIFF",
    ),
    (
        "animation.gif",
        lambda path: media_assets.write_gif(path, PALETTE, 42),
        {"min_frames": 8, "min_duration_ms": 640},
        b"GIF89a",
    ),
    (
        "score.mid",
        lambda path: media_assets.write_midi(path, "Arcane Suite", "arcane", 42),
        {"min_notes": 16, "require_tempo": True},
        b"MThd",
    ),
    (
        "captions.srt",
        lambda path: media_assets.write_srt(path, "An arcane launch sequence"),
        {"min_cues": 6, "required_text": ["arcane launch"]},
        b"1\n00:00:00,000",
    ),
    (
        "captions.vtt",
        lambda path: media_assets.write_vtt(path, "An arcane launch sequence"),
        {"min_cues": 6, "required_text": ["arcane launch"]},
        b"WEBVTT",
    ),
    (
        "timeline.edl",
        lambda path: media_assets.write_edl(
            path, "Arcane Suite", "An arcane launch sequence"
        ),
        {"min_events": 6, "required_text": ["Arcane Suite"]},
        b"TITLE: Arcane Suite",
    ),
]


@pytest.mark.parametrize("filename,writer,requirements,signature", CASES)
def test_media_outputs_are_deterministic_and_format_grounded(
    tmp_path, filename, writer, requirements, signature
):
    first = tmp_path / "first" / filename
    second = tmp_path / "second" / filename

    writer(first)
    writer(second)

    first_data = first.read_bytes()
    assert first_data.startswith(signature)
    assert hashlib.sha256(first_data).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()
    result = artifact_grounding.validate(first, "auto", requirements)
    assert result["ok"], artifact_grounding.format_result(result)


def test_media_seed_and_theme_change_animation_and_score(tmp_path):
    first_gif = tmp_path / "first.gif"
    second_gif = tmp_path / "second.gif"
    first_midi = tmp_path / "first.mid"
    second_midi = tmp_path / "second.mid"
    frost = ((13, 24, 38), (69, 147, 203), (194, 235, 255), (39, 73, 103))

    media_assets.write_gif(first_gif, PALETTE, 1)
    media_assets.write_gif(second_gif, frost, 2)
    media_assets.write_midi(first_midi, "First", "arcane", 1)
    media_assets.write_midi(second_midi, "Second", "frost", 2)

    assert first_gif.read_bytes() != second_gif.read_bytes()
    assert first_midi.read_bytes() != second_midi.read_bytes()


def test_avi_contains_distinct_frames_and_non_silent_pcm(tmp_path):
    video = tmp_path / "preview.avi"
    media_assets.write_avi(video, PALETTE, "arcane", 42)
    data = video.read_bytes()
    frames = []
    audio = bytearray()
    for kind, payload_offset, size, _chunk_offset in artifact_grounding._riff_chunks(
        data, 12, len(data)
    ):
        if kind != b"LIST" or data[payload_offset : payload_offset + 4] != b"movi":
            continue
        for child, child_offset, child_size, _child_chunk in artifact_grounding._riff_chunks(
            data, payload_offset + 4, payload_offset + size
        ):
            if child == b"00db":
                frames.append(hashlib.sha256(data[child_offset : child_offset + child_size]).digest())
            elif child == b"01wb":
                audio.extend(data[child_offset : child_offset + child_size])

    assert len(frames) == 48
    assert len(set(frames)) >= 24
    assert len(audio) == 48 * 1000 * 2
    assert any(audio)


def test_avi_grounding_rejects_inconsistent_audio_stream_length(tmp_path):
    video = tmp_path / "tampered.avi"
    media_assets.write_avi(video, PALETTE, "arcane", 42)
    data = bytearray(video.read_bytes())
    audio_header = data.find(b"auds")
    assert audio_header > 0
    declared_samples = struct.unpack_from("<I", data, audio_header + 32)[0]
    struct.pack_into("<I", data, audio_header + 32, declared_samples + 1)
    video.write_bytes(data)

    result = artifact_grounding.validate(video, "avi", {"require_audio": True})

    assert not result["ok"]
    assert any(
        item["name"] == "valid-avi" and not item["ok"]
        for item in result["checks"]
    )


def test_media_writers_replace_outputs_atomically_without_temp_files(tmp_path):
    targets = [tmp_path / item[0] for item in CASES]
    for target, (_filename, writer, _requirements, _signature) in zip(targets, CASES):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"old")
        writer(target)
        assert target.read_bytes() != b"old"
        assert not Path(str(target) + ".tmp").exists()
