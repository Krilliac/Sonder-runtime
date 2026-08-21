from __future__ import annotations

import pytest

from sonder_runtime.application.ports.vision_gateway import (
    MAX_VISION_BYTES,
    MAX_VISION_PROMPT_CHARS,
    VisionRequest,
    require_vision_text,
)


def _request(**overrides):
    values = {
        "prompt": "describe the image",
        "image": b"PNG-bytes",
        "media_type": "image/png",
    }
    values.update(overrides)
    return VisionRequest(**values)


def test_vision_request_is_bounded_and_typed():
    request = _request()
    assert request.tier == "vision"
    assert request.image == b"PNG-bytes"


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", "x" * (MAX_VISION_PROMPT_CHARS + 1)),
        ("image", b"x" * (MAX_VISION_BYTES + 1)),
        ("media_type", "image/gif"),
        ("image", "not-bytes"),
    ],
    ids=["prompt-too-long", "image-too-large", "gif", "image-not-bytes"],
)
def test_vision_request_rejects_unbounded_or_unsupported_inputs(field, value):
    with pytest.raises(ValueError):
        _request(**{field: value})


def test_vision_output_requires_nonempty_text():
    assert require_vision_text(" answer ") == " answer "
    with pytest.raises(ValueError):
        require_vision_text(" ")
