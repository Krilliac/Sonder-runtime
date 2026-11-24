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


def test_concept_image_helper_calls_openai_image_api(monkeypatch):
    import sonder_runtime.adapters.image_generation as img_gen

    monkeypatch.setenv("SONDER_IMAGE_API_URL", "http://127.0.0.1:11440")
    fake_backend = img_gen.ImageBackend(
        kind="openai_api", model="sdxl-turbo", endpoint="http://127.0.0.1:11440/v1/images/generations",
    )
    monkeypatch.setattr(img_gen, "discover_image_backend", lambda: fake_backend)
    seen = {}

    def _fake_call(backend, prompt, *, timeout):
        seen["endpoint"] = backend.endpoint
        seen["prompt"] = prompt
        seen["timeout"] = timeout
        return b"\x89PNG\r\n\x1a\nfake", "image/png"

    monkeypatch.setattr(img_gen, "_call_openai_image_api", _fake_call)
    got = adapter._generate_concept_image_bytes("a crystal sword")
    assert got == (b"\x89PNG\r\n\x1a\nfake", "image/png")
    assert "a crystal sword" in seen["prompt"]
    assert "white studio background" in seen["prompt"]


@pytest.mark.parametrize(
    "raw_prompt,expected_subject,expect_boost",
    [
        ("generate a highly detailed 3d model of your choosing", app_m3d.CREATIVE_DEFAULT_3D_SUBJECT, True),
        ("highly your choosing", app_m3d.CREATIVE_DEFAULT_3D_SUBJECT, True),
        ("of your choosing", app_m3d.CREATIVE_DEFAULT_3D_SUBJECT, False),
        ("your choice", app_m3d.CREATIVE_DEFAULT_3D_SUBJECT, False),
        ("anything", app_m3d.CREATIVE_DEFAULT_3D_SUBJECT, False),
        ("surprise me", app_m3d.CREATIVE_DEFAULT_3D_SUBJECT, False),
        ("highly detailed medieval sword", "medieval sword", True),
        ("ultra detailed brass compass", "brass compass", True),
        ("high detail wooden shield", "wooden shield", True),
        ("highly a crystal dragon", "a crystal dragon", True),
    ],
)
def test_parse_model3d_prompt_strips_quality_modifiers_and_replaces_open_ended(
    raw_prompt, expected_subject, expect_boost,
):
    opts = app_m3d.parse_model3d_prompt(raw_prompt)
    assert opts.prompt == expected_subject
    assert "highly your choosing" not in opts.prompt
    if expect_boost:
        assert opts.steps == app_m3d.HIGH_DETAIL_3D_STEPS
        assert opts.octree_resolution == app_m3d.HIGH_DETAIL_3D_OCTREE_RESOLUTION
        assert opts.face_count == app_m3d.HIGH_DETAIL_3D_FACE_COUNT
    else:
        assert opts.steps is None
        assert opts.octree_resolution is None
        assert opts.face_count is None


def test_loopback_opener_rejects_302_and_307_redirects():
    import http.server
    import threading
    import urllib.error
    import urllib.request

    redirected_hits = []

    class _TargetHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            redirected_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"secret")

        def log_message(self, *_a):
            pass

    target_srv = http.server.HTTPServer(("127.0.0.1", 0), _TargetHandler)
    target_port = target_srv.server_address[1]
    t1 = threading.Thread(target=target_srv.serve_forever, daemon=True)
    t1.start()

    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code = 307 if self.path.endswith("307") else 302
            self.send_response(code)
            self.send_header("Location", f"http://127.0.0.1:{target_port}/leaked")
            self.end_headers()

        def log_message(self, *_a):
            pass

    redir_srv = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    redir_port = redir_srv.server_address[1]
    t2 = threading.Thread(target=redir_srv.serve_forever, daemon=True)
    t2.start()

    try:
        opener = adapter._loopback_opener()
        for suffix, expected_code in (("302", 302), ("307", 307)):
            req = urllib.request.Request(f"http://127.0.0.1:{redir_port}/{suffix}", method="GET")
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                opener.open(req, timeout=5)
            assert excinfo.value.code == expected_code
        assert redirected_hits == []
    finally:
        redir_srv.shutdown()
        redir_srv.server_close()
        target_srv.shutdown()
        target_srv.server_close()


def test_hunyuan3d_bearer_auth_and_explicit_kwarg_clamping_and_disk_quota(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("SONDER_3D_API_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("SONDER_3D_API_KEY", "super-secret-3d-token-xyz")
    monkeypatch.setenv("SONDER_GENERATED_MODELS_MAX_BYTES", "500")
    monkeypatch.delenv("SONDER_3D_YIELD_LLAMASERVER_URL", raising=False)

    models_dir = tmp_path / "generated-models"
    models_dir.mkdir(parents=True, exist_ok=True)
    old_file = models_dir / "old-model-20200101-000000-aaaaaa.glb"
    old_file.write_bytes(b"X" * 450)

    seen_auth: list[tuple[str, str | None]] = []
    seen_payloads: list[dict] = []

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body
            self.status = 200

        def read(self, _n: int = -1) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            seen_auth.append((req.full_url, req.get_header("Authorization")))
            if req.full_url.endswith("/health"):
                return _Resp(json.dumps({"status": "ok", "tex_enabled": True, "t23d_enabled": True}).encode())
            if req.full_url.endswith("/generate"):
                seen_payloads.append(json.loads(req.data.decode("utf-8")))
                return _Resp(b"glTF" + b"\x00" * 200)
            raise AssertionError(req.full_url)

    monkeypatch.setattr(adapter, "_loopback_opener", lambda: _Opener())
    monkeypatch.setattr(adapter, "_generate_concept_image_bytes", lambda _p: None)
    monkeypatch.setattr(adapter, "render_glb_preview", lambda *_a, **_kw: {"ok": False, "error": "skip"})

    res = adapter.generate_chat_3d_model(
        "brass lantern",
        steps=9999,
        octree_resolution=4096,
        face_count=9_000_000,
    )
    assert Path(res.model_path).is_file()
    # Auth header attached on both /health and /generate
    assert all(hdr == "Bearer super-secret-3d-token-xyz" for _, hdr in seen_auth)
    assert len(seen_auth) >= 2
    # Explicit kwargs clamped to safe bounds (M-03)
    assert seen_payloads[0]["num_inference_steps"] == 50
    assert seen_payloads[0]["octree_resolution"] == 512
    assert seen_payloads[0]["face_count"] == 200_000
    # Rolling quota evicted older model while keeping the newly written one
    assert not old_file.exists()


def test_render_glb_preview_sanitizes_environment_and_isolates_cwd(monkeypatch, tmp_path):
    fake_blender = tmp_path / "blender.exe"
    fake_blender.write_bytes(b"stub")
    monkeypatch.setenv("SONDER_BLENDER", str(fake_blender))
    monkeypatch.setenv("SONDER_API_KEY", "leaked-api-key-12345")
    monkeypatch.setenv("SONDER_3D_API_KEY", "leaked-3d-key-12345")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leaked-secret-value")

    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        job_path = Path(cmd[-1])
        job = json.loads(job_path.read_text(encoding="utf-8"))
        Path(job["result_path"]).write_text(json.dumps({"ok": True, "tris": 100}), encoding="utf-8")
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adapter.subprocess, "run", _fake_run)
    out = adapter.render_glb_preview(tmp_path / "m.glb", tmp_path / "p.png")
    assert out["ok"] is True
    env = captured["kwargs"]["env"]
    assert "SONDER_API_KEY" not in env
    assert "SONDER_3D_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] != str(Path.cwd())


def test_serve_media_generation_errors_return_sanitized_message(monkeypatch):
    import sonder_runtime.interfaces.http.serve as http_serve

    monkeypatch.setattr(
        http_serve._model3d_generation,
        "generate_chat_3d_model",
        lambda _p: (_ for _ in ()).throw(RuntimeError("Local Hunyuan3D server is unavailable (secret=sk-abc123456789012345)")),
    )
    msg = http_serve._format_media_generation_error(
        "3d", RuntimeError("Local Hunyuan3D server is unavailable (secret=sk-abc123456789012345)"),
    )
    assert "unavailable" in msg.lower()
    assert "sk-abc" not in msg
    assert "Traceback" not in msg

    img_msg = http_serve._format_media_generation_error(
        "image", ValueError("generated image exceeds inline byte budget"),
    )
    assert "inline size budget" in img_msg.lower()
    gif_msg = http_serve._format_media_generation_error("gif", OSError("disk full"))
    assert "gif generation failed" in gif_msg.lower()
    assert "disk full" not in gif_msg
