"""Tests for animated GIF intent classification, GIF89a synthesis, and chat routing."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import struct
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import intents
from sonder_runtime.application import image_generation


def _make_test_png(width: int = 128, height: int = 128, *, filter_type: int = 0) -> bytes:
    """Build a valid 8-bit RGB PNG with a colorful diagonal pattern (stdlib only)."""
    stride = width * 3
    prev = bytearray(stride)
    raw = bytearray()
    for y in range(height):
        row = bytearray(stride)
        for x in range(width):
            r = (x * 255) // max(1, width - 1)
            g = (y * 255) // max(1, height - 1)
            b = ((x * 3 + y * 5) % 256)
            off = x * 3
            row[off] = r
            row[off + 1] = g
            row[off + 2] = b
        raw.append(filter_type)
        if filter_type == 0:
            raw.extend(row)
        elif filter_type == 1:
            for i in range(stride):
                left = row[i - 3] if i >= 3 else 0
                raw.append((row[i] - left) & 0xFF)
        elif filter_type == 4:
            for i in range(stride):
                a = row[i - 3] if i >= 3 else 0
                b = prev[i]
                c = prev[i - 3] if i >= 3 else 0
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                raw.append((row[i] - pr) & 0xFF)
        else:
            raw.extend(row)
        prev = row

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )


_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, _limit: int = -1) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeOpener:
    def __init__(self, payload: dict):
        self._payload = payload
        self.requests: list[tuple[str, dict]] = []

    def open(self, req, timeout=30):
        body = json.loads(req.data.decode("utf-8")) if getattr(req, "data", None) else {}
        self.requests.append((req.full_url, body))
        return _FakeResponse(self._payload)


def _extract_inline_gif_bytes(markdown: str) -> bytes:
    match = re.search(r"data:image/gif;base64,([A-Za-z0-9+/=]+)", markdown)
    assert match is not None, f"expected inline data:image/gif URI in markdown: {markdown[:200]!r}"
    return base64.b64decode(match.group(1))


class GifIntentTests(unittest.TestCase):
    def test_triggers_on_slash_and_natural_gif_prompts(self):
        self.assertTrue(intents.classify_gif_generation("can you generate a gif?"))
        self.assertEqual(intents.classify_gif_generation("/gif neon waves"), "neon waves")
        self.assertTrue(intents.classify_gif_generation("/gif"))

        cat_prompt = intents.classify_gif_generation("generate a gif of a cyberpunk cat")
        self.assertIsNotNone(cat_prompt)
        self.assertIn("cyberpunk cat", cat_prompt.lower())

        for text in (
            "create an animated gif of bioluminescent jellyfish",
            "make a gif of ocean waves at sunset",
            "please generate a looping gif",
            "make a cyberpunk neon gif",
            "could you create an animated gif of a dragon, please?",
            "render a gif showing aurora borealis",
        ):
            result = intents.classify_gif_generation(text)
            self.assertIsNotNone(result, f"expected match for {text!r}")

    def test_rejects_conversion_code_file_and_non_gif_prompts(self):
        for text in (
            "how do I convert mp4 to gif in ffmpeg?",
            "write python code to convert png to gif",
            "write a python script to generate an animated gif",
            "how does the gif89a format work?",
            "explain how animated gifs compress frames",
            "fix the gif playback bug in flutter",
            "create a gif picker widget in dart",
            "generate a gif and save it to assets/demo.gif",
            "make a gif encoder in rust",
            "convert video.mp4 to output.gif",
            "generate a random image",
            "draw a cute robot holding coffee",
            "/image neon waves",
            "/gift ideas",
        ):
            result = intents.classify_gif_generation(text)
            self.assertIsNone(result, f"expected None for {text!r}, got {result!r}")


class GifGenerationTests(unittest.TestCase):
    def _assert_valid_animated_gif(self, raw: bytes, *, min_frames: int = 8) -> None:
        self.assertTrue(raw.startswith(b"GIF89a"), "GIF bytes must start with GIF89a header")
        self.assertIn(b"NETSCAPE2.0", raw[:900], "GIF should embed NETSCAPE2.0 looping extension")
        self.assertEqual(raw[-1], 0x3B, "GIF must end with 0x3B trailer")
        self.assertLess(len(raw), image_generation.MAX_INLINE_IMAGE_BYTES)

        payloads = image_generation.extract_gif_frame_payloads(raw)
        self.assertGreaterEqual(len(payloads), min_frames)
        self.assertGreaterEqual(
            len(set(payloads)),
            min_frames,
            f"expected >= {min_frames} distinct frames, got {len(set(payloads))}",
        )

        # When Pillow is available in the current interpreter, also verify C decoding.
        try:
            from PIL import Image
        except ImportError:
            return
        img = Image.open(io.BytesIO(raw))
        self.assertEqual(img.format, "GIF")
        self.assertTrue(getattr(img, "is_animated", False))
        self.assertGreaterEqual(img.n_frames, min_frames)
        rgb_frames = set()
        for idx in range(img.n_frames):
            img.seek(idx)
            rgb_frames.add(img.convert("RGB").tobytes())
        self.assertGreaterEqual(len(rgb_frames), min_frames)

    def test_procedural_gif_fallback_generates_valid_looping_gif(self):
        def fake_open_url(req, timeout=30, **_kwargs):
            return _FakeResponse({"models": [{"name": "sonder:latest"}]})

        with TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SONDER_IMAGE_API_URL": "", "SONDER_IMAGE_MODEL": ""}, clear=False), \
                 mock.patch.object(image_generation.ollama_endpoint, "open_url", side_effect=fake_open_url):
                result = image_generation.generate_chat_gif(
                    "neon waves",
                    state_home=tmp,
                    ollama_endpoints=("http://127.0.0.1:11434",),
                )

            self.assertIsInstance(result, image_generation.ImageGenerationResult)
            self.assertEqual(result.mime_type, "image/gif")
            self.assertEqual(result.backend_kind, "assetgen")
            self.assertTrue(result.used_fallback)
            self.assertGreater(result.width, 0)
            self.assertGreater(result.height, 0)
            self.assertGreaterEqual(result.frame_count, 10)
            self.assertIn("](data:image/gif;base64,", result.markdown)
            self.assertIn("Generated animated GIF via `", result.markdown)
            self.assertIn(f"{result.frame_count} frames)", result.markdown)

            saved = Path(result.saved_path)
            self.assertTrue(saved.is_file())
            self.assertEqual(saved.suffix, ".gif")
            self.assertTrue(saved.is_relative_to(Path(tmp) / "generated-images"))

            raw = saved.read_bytes()
            self.assertEqual(raw, _extract_inline_gif_bytes(result.markdown))
            self._assert_valid_animated_gif(raw, min_frames=10)

            cleaned = image_generation.strip_inline_data_images(result.markdown)
            self.assertNotIn("data:image/", cleaned)
            self.assertIn("[Generated image: neon waves]", cleaned)
            self.assertIn("Generated animated GIF via", cleaned)
            self.assertLess(len(cleaned), 500)

    def test_generic_gif_prompt_synthesizes_vivid_looping_gif(self):
        def fake_open_url(req, timeout=30, **_kwargs):
            return _FakeResponse({"models": []})

        gif_prompt = intents.classify_gif_generation("can you generate a gif?")
        self.assertIsNotNone(gif_prompt)

        with TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SONDER_IMAGE_API_URL": "", "SONDER_IMAGE_MODEL": ""}, clear=False), \
                 mock.patch.object(image_generation.ollama_endpoint, "open_url", side_effect=fake_open_url):
                result = image_generation.generate_chat_gif(
                    gif_prompt,
                    state_home=tmp,
                    ollama_endpoints=("http://127.0.0.1:11434",),
                )

            saved = Path(result.saved_path)
            self.assertTrue(saved.is_file())
            self._assert_valid_animated_gif(saved.read_bytes(), min_frames=10)

    def test_openai_image_api_keyframe_animates_to_looping_gif(self):
        keyframe_png = _make_test_png(192, 192, filter_type=4)
        opener = _FakeOpener({"data": [{"b64_json": base64.b64encode(keyframe_png).decode("ascii")}]})

        with TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"SONDER_IMAGE_API_URL": "http://127.0.0.1:11440", "SONDER_IMAGE_MODEL": "sdxl-turbo"},
                clear=False,
            ), mock.patch.object(image_generation.urllib.request, "build_opener", return_value=opener):
                result = image_generation.generate_chat_gif(
                    "a cyberpunk cat",
                    state_home=tmp,
                )

            self.assertEqual(result.backend_kind, "openai_api")
            self.assertEqual(result.model_id, "sdxl-turbo")
            self.assertFalse(result.used_fallback)
            self.assertEqual(result.mime_type, "image/gif")
            self.assertIn("Generated animated GIF via `sdxl-turbo`", result.markdown)
            self.assertEqual(len(opener.requests), 1)
            self.assertEqual(opener.requests[0][0], "http://127.0.0.1:11440/v1/images/generations")

            saved = Path(result.saved_path)
            self.assertTrue(saved.is_file())
            self.assertEqual(saved.suffix, ".gif")
            self._assert_valid_animated_gif(saved.read_bytes(), min_frames=8)

    def test_pure_python_png_animator_works_without_pillow(self):
        keyframe_png = _make_test_png(160, 128, filter_type=1)
        opener = _FakeOpener({"data": [{"b64_json": base64.b64encode(keyframe_png).decode("ascii")}]})

        with TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"SONDER_IMAGE_API_URL": "http://127.0.0.1:11440", "SONDER_IMAGE_MODEL": "sdxl-turbo"},
                clear=False,
            ), mock.patch.object(
                image_generation.urllib.request, "build_opener", return_value=opener,
            ), mock.patch.object(
                image_generation, "_animate_keyframe_pillow", return_value=None,
            ):
                result = image_generation.generate_chat_gif(
                    "neon cyberpunk skyline",
                    state_home=tmp,
                )

            self.assertEqual(result.backend_kind, "openai_api")
            self.assertEqual(result.model_id, "sdxl-turbo")
            self.assertFalse(result.used_fallback)
            self._assert_valid_animated_gif(Path(result.saved_path).read_bytes(), min_frames=8)

    def test_tiny_keyframe_still_produces_distinct_animated_frames(self):
        opener = _FakeOpener({"data": [{"b64_json": base64.b64encode(_TINY_PNG).decode("ascii")}]})

        with TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"SONDER_IMAGE_API_URL": "http://127.0.0.1:11440", "SONDER_IMAGE_MODEL": "sdxl-turbo"},
                clear=False,
            ), mock.patch.object(image_generation.urllib.request, "build_opener", return_value=opener):
                result = image_generation.generate_chat_gif("red pulse", state_home=tmp)

            self._assert_valid_animated_gif(Path(result.saved_path).read_bytes(), min_frames=8)

    def test_backend_error_falls_back_cleanly_to_procedural_gif(self):
        class _FailingOpener:
            def open(self, _req, timeout=30):
                raise OSError("connection refused")

        with TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"SONDER_IMAGE_API_URL": "http://127.0.0.1:11440", "SONDER_IMAGE_MODEL": "sdxl-turbo"},
                clear=False,
            ), mock.patch.object(image_generation.urllib.request, "build_opener", return_value=_FailingOpener()):
                result = image_generation.generate_chat_gif("aurora borealis", state_home=tmp)

            self.assertTrue(result.used_fallback)
            self.assertEqual(result.backend_kind, "assetgen")
            self.assertIn("procedural animated GIF fallback", result.fallback_note)
            self._assert_valid_animated_gif(Path(result.saved_path).read_bytes(), min_frames=10)

    def test_inline_gif_stripping_and_context_accounting(self):
        from sonder_runtime.adapters import memory_store

        big_gif_md = (
            "![neon waves](data:image/gif;base64,"
            + ("R" * 450_000)
            + ")\n\nGenerated animated GIF via `sdxl-turbo` (384x384, 14 frames)."
        )
        cleaned = image_generation.strip_inline_data_images(big_gif_md)
        self.assertNotIn("data:image/", cleaned)
        self.assertIn("[Generated image: neon waves]", cleaned)
        self.assertIn("Generated animated GIF via `sdxl-turbo`", cleaned)
        self.assertLess(len(cleaned), 200)

        tokens_in, tokens_out = memory_store.estimate_interaction_tokens(
            "/gif neon waves", "", big_gif_md,
        )
        self.assertLess(tokens_in, 30)
        self.assertLess(tokens_out, 50)


class GifSlashRouteTests(unittest.TestCase):
    def test_http_handle_slash_routes_gif_command(self):
        from sonder_runtime.interfaces.http import serve

        fake_result = image_generation.GeneratedImageResult(
            markdown="![neon waves](data:image/gif;base64,R0lGODlh)\n\nGenerated animated GIF via `sonder-gifgen` (320x200, 14 frames).",
            backend_kind="assetgen",
            model_id="sonder-gifgen",
            mime_type="image/gif",
            width=320,
            height=200,
            byte_length=6,
            saved_path="/tmp/neon-waves.gif",
            used_fallback=True,
            frame_count=14,
        )
        local_ctx = {"mode": "local-open", "authorized": True, "api_key": False, "account": None}
        with mock.patch.object(serve._image_generation, "generate_chat_gif", return_value=fake_result) as mocked:
            reply = serve._handle_slash("/gif neon waves", context=local_ctx)
            self.assertEqual(reply, fake_result.markdown)
            mocked.assert_called_once_with("neon waves")

            mocked.reset_mock()
            reply_default = serve._handle_slash("/gif", context=local_ctx)
            self.assertEqual(reply_default, fake_result.markdown)
            mocked.assert_called_once_with("a random animated gif")


if __name__ == "__main__":
    unittest.main()
