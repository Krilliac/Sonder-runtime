import json
import math

import pytest

import npu_contract as c


def test_encode_line_is_compact_json_with_newline():
    raw = c.encode_line({"id": 1, "op": "ping"})
    assert raw.endswith(b"\n")
    assert b" " not in raw.split(b"\n")[0].replace(b'"op": ', b"")  # compact separators
    assert json.loads(raw.decode("utf-8")) == {"id": 1, "op": "ping"}


def test_encode_line_rejects_oversized_payload():
    with pytest.raises(ValueError):
        c.encode_line({"blob": "x" * (c.MAX_LINE_BYTES + 1)})


def test_encode_line_rejects_non_finite_floats():
    with pytest.raises(ValueError):
        c.encode_line({"score": float("nan")})
    with pytest.raises(ValueError):
        c.encode_line({"nested": [{"v": float("inf")}]})


def test_decode_line_roundtrip():
    assert c.decode_line(c.encode_line({"id": 7, "ok": True})) == {"id": 7, "ok": True}


def test_decode_line_rejects_oversized_line():
    raw = (b'{"pad": "' + b"y" * c.MAX_LINE_BYTES + b'"}\n')
    with pytest.raises(ValueError):
        c.decode_line(raw)


def test_decode_line_rejects_non_object_and_garbage():
    with pytest.raises(ValueError):
        c.decode_line(b"[1, 2, 3]\n")
    with pytest.raises(ValueError):
        c.decode_line(b"not json at all\n")
    with pytest.raises(ValueError):
        c.decode_line(b"\xff\xfe binary\n")


def test_decode_line_rejects_nan_and_infinity_literals():
    with pytest.raises(ValueError):
        c.decode_line(b'{"score": NaN}\n')
    with pytest.raises(ValueError):
        c.decode_line(b'{"score": Infinity}\n')
    with pytest.raises(ValueError):
        c.decode_line(b'{"score": -Infinity}\n')


def test_validate_route_scores_accepts_allowlisted_payload():
    result = c.validate_route_scores({
        "scores": {"workbench": 0.7, "autopilot": 0.3},
        "reason_code": "score_margin",
    })
    assert result["scores"]["workbench"] == pytest.approx(0.7)
    assert result["reason_code"] == "score_margin"


def test_validate_route_scores_rejects_unknown_labels():
    with pytest.raises(ValueError):
        c.validate_route_scores({
            "scores": {"workbench": 0.5, "cloud": 0.5},
            "reason_code": "score_margin",
        })


def test_validate_route_scores_requires_every_mode_label():
    with pytest.raises(ValueError):
        c.validate_route_scores({
            "scores": {"workbench": 1.0},
            "reason_code": "score_margin",
        })


def test_validate_route_scores_rejects_non_finite_and_out_of_range():
    for bad in (float("nan"), float("inf"), -0.1, 1.5):
        with pytest.raises(ValueError):
            c.validate_route_scores({
                "scores": {"workbench": bad, "autopilot": 0.5},
                "reason_code": "score_margin",
            })


def test_validate_route_scores_rejects_unlisted_reason_code():
    with pytest.raises(ValueError):
        c.validate_route_scores({
            "scores": {"workbench": 0.6, "autopilot": 0.4},
            "reason_code": "because the model said so",
        })


def test_validate_route_scores_rejects_bool_scores():
    with pytest.raises(ValueError):
        c.validate_route_scores({
            "scores": {"workbench": True, "autopilot": 0.0},
            "reason_code": "score_margin",
        })


def test_validate_vector_enforces_dimension_and_finiteness():
    vector = c.validate_vector([0.1, 0.2, 0.3], 3)
    assert vector == pytest.approx([0.1, 0.2, 0.3])
    with pytest.raises(ValueError):
        c.validate_vector([0.1, 0.2], 3)
    with pytest.raises(ValueError):
        c.validate_vector([0.1, float("nan"), 0.3], 3)
    with pytest.raises(ValueError):
        c.validate_vector([0.0, 0.0, 0.0], 3)
    with pytest.raises(ValueError):
        c.validate_vector([1.0] * (c.MAX_DIMENSION + 1), c.MAX_DIMENSION + 1)


def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib

    target = tmp_path / "model.onnx"
    target.write_bytes(b"weights-bytes")
    assert c.sha256_file(target) == hashlib.sha256(b"weights-bytes").hexdigest()


def test_clamp_deadline_bounds_route_and_embed():
    assert c.clamp_deadline_ms(10, "routing") == c.MIN_DEADLINE_MS
    assert c.clamp_deadline_ms(99999, "routing") == c.MAX_ROUTE_DEADLINE_MS
    assert c.clamp_deadline_ms(99999, "embedding") == c.MAX_EMBED_DEADLINE_MS
    assert c.clamp_deadline_ms(None, "routing") == c.DEFAULT_ROUTE_DEADLINE_MS
    assert c.clamp_deadline_ms(None, "embedding") == c.DEFAULT_EMBED_DEADLINE_MS
