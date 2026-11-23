"""Unit tests for Hunyuan3D-2 3D model generation, NL intent routing, and safe download paths."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest import mock

import pytest

import intents
from sonder_runtime.adapters import model3d_generation as adapter
from sonder_runtime.application import model3d_generation as app_m3d

pytestmark = pytest.mark.unit


def test_parse_model3d_prompt_extracts_flags_and_clamps():
    opts = app_m3d.parse_model3d_prompt(
        'a medieval iron lantern notex steps=99 octree=128 faces=25000 seed=42 format=obj'
    )
    assert opts.prompt == "a medieval iron lantern"
    assert opts.texture is False
    assert opts.steps == 50
    assert opts.octree_resolution == 128
    assert opts.face_count == 25000
    assert opts.seed == 42
    assert opts.file_format == "obj"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("lantern-20261123-120000-abcdef.glb", True),
        ("crate.obj", True),
        ("preview.png", True),
        ("../secret.glb", False),
        ("sub/model.glb", False),
        ("sub\\model.glb", False),
        ("payload.exe", False),
        ("", False),
    ],
)
def test_is_safe_model_filename(name, expected):
    assert app_m3d.is_safe_model_filename(name) is expected


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("/3d a crystal sword", "a crystal sword"),
        ("/model3d medieval lantern notex", "medieval lantern notex"),
        ("/3d", "a stylized glowing lantern"),
        ("make me a 3d model of a medieval iron lantern", "a medieval iron lantern"),
        ("Can you generate a 3D mesh of a cute robot?", "a cute robot"),
        ("create a hunyuan3d model of a pirate treasure chest", "a pirate treasure chest"),
        ("sculpt a 3d figurine of a cybernetic fox", "a cybernetic fox"),
        ("build a glb model of a sci-fi crate", "a sci-fi crate"),
    ],
)
def test_classify_3d_model_generation_positive(prompt, expected):
    assert intents.classify_3d_model_generation(prompt) == expected


@pytest.mark.parametrize(
    "prompt",
    [
        "how does hunyuan3d work?",
        "write a three.js 3d model viewer in typescript",
        "create a blender python bpy script to make a 3d cube",
        "explain the difference between glb and fbx 3d models",
        "make a 3d shader in glsl for water",
        "what is a 3d mesh?",
        "generate an image of a cat",
    ],
)
def test_classify_3d_model_generation_negative(prompt):
    assert intents.classify_3d_model_generation(prompt) is None


def test_discover_3d_backend_rejects_non_loopback_http(monkeypatch):
    monkeypatch.setenv("SONDER_3D_API_URL", "http://192.168.1.99:8081")
    backend = adapter.discover_3d_backend()
    assert backend.kind == "unavailable"
    assert "https" in backend.detail.lower()


def test_generate_chat_3d_model_saves_glb_and_renders_preview(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("SONDER_3D_API_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("SONDER_3D_YIELD_LLAMASERVER_URL", "http://127.0.0.1:18080")

    fake_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    fake_glb = b"glTF\x02\x00\x00\x00" + b"\x00" * 128

    seen_requests: list[tuple[str, dict | None]] = []

    class _Resp:
        def __init__(self, body: bytes, status: int = 200):
            self._body = body
            self.status = status

        def read(self, _n: int = -1) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            body = json.loads(req.data.decode("utf-8")) if req.data else None
            seen_requests.append((req.full_url, body))
            if req.full_url.endswith("/health"):
                return _Resp(json.dumps({"status": "ok", "subfolder": "hunyuan3d-dit-v2-mini-turbo", "tex_enabled": True, "t23d_enabled": True}).encode())
            if req.full_url.endswith("/models"):
                return _Resp(json.dumps({"data": [{"id": "bonsai-27b-pq2", "status": {"value": "loaded"}}]}).encode())
            if req.full_url.endswith("/models/unload"):
                return _Resp(b'{"status":"ok"}')
            if req.full_url.endswith("/generate"):
                return _Resp(fake_glb)
            raise AssertionError(f"unexpected URL: {req.full_url}")

    monkeypatch.setattr(adapter, "_loopback_opener", lambda: _Opener())
    monkeypatch.setattr(adapter, "_generate_concept_image_bytes", lambda _p: (fake_png, "image/png"))

    def _fake_render(model_path: Path, sheet_path: Path, **_kw):
        sheet_path.write_bytes(fake_png)
        return {"ok": True, "preview_path": str(sheet_path), "tris": 12480, "verts": 6310, "materials": 1}

    monkeypatch.setattr(adapter, "render_glb_preview", _fake_render)

    with mock.patch("time.sleep", return_value=None):
        res = adapter.generate_chat_3d_model("a medieval iron lantern")

    assert res.backend_kind == "hunyuan3d"
    assert res.tris == 12480
    assert res.verts == 6310
    assert res.model_bytes == len(fake_glb)
    assert Path(res.model_path).read_bytes() == fake_glb
    assert res.download_url == f"/v1/generated-models/{res.model_filename}"
    assert adapter.resolve_generated_model_path(res.model_filename) == Path(res.model_path).resolve()
    assert adapter.resolve_generated_model_path("../escape.glb") is None
    assert "![3D preview: a medieval iron lantern](data:image/png;base64," in res.markdown
    assert f"[{res.model_filename}" in res.markdown
    assert f"({res.download_url})" in res.markdown

    urls = [u for u, _ in seen_requests]
    assert "http://127.0.0.1:18080/models/unload" in urls
    gen_body = next(b for u, b in seen_requests if u.endswith("/generate"))
    assert gen_body["texture"] is True
    assert "image" in gen_body

    ticket = adapter.mint_model_download_ticket(res.model_filename)
    assert adapter.verify_model_download_ticket(res.model_filename, ticket) is True
    assert adapter.verify_model_download_ticket("other.glb", ticket) is False
    assert adapter.verify_model_download_ticket(res.model_filename, "0.deadbeef") is False
    upgraded = adapter.attach_signed_download_url(
        res.markdown, res.model_filename, origin="http://127.0.0.1:11435",
    )
    assert f"(http://127.0.0.1:11435/v1/generated-models/{res.model_filename}?dl=" in upgraded
