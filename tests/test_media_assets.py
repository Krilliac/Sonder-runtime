import hashlib
from pathlib import Path

import pytest

import artifact_grounding
import media_assets


PALETTE = ((17, 15, 35), (116, 91, 218), (87, 218, 207), (49, 38, 91))


CASES = [
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


def test_media_writers_replace_outputs_atomically_without_temp_files(tmp_path):
    targets = [tmp_path / item[0] for item in CASES]
    for target, (_filename, writer, _requirements, _signature) in zip(targets, CASES):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"old")
        writer(target)
        assert target.read_bytes() != b"old"
        assert not Path(str(target) + ".tmp").exists()
