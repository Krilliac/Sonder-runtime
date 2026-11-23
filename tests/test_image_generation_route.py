"""Tests for dedicated image-model routing, inline PNG replies, and SSE chunking."""
from __future__ import annotations

import base64
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import intents
from sonder_runtime.adapters import image_generation
from sonder_runtime.interfaces.http import live_stream


# Minimal valid 1x1 red PNG
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


class _FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()


class ImageIntentTests(unittest.TestCase):
    def test_matches_direct_image_prompts(self):
        for text in (
            "generate a random image",
            "please generate me an image of a neon skyline",
            "Can you draw a cute robot holding coffee?",
            "create a cyberpunk wallpaper",
            "paint an oil painting of misty mountains",
            "render a 3d illustration of a glowing crystal",
            "make me a picture of a cat in space",
        ):
            result = intents.classify_image_generation(text)
            self.assertIsNotNone(result, f"expected match for {text!r}")

    def test_rejects_code_file_and_question_prompts(self):
        for text in (
            "how do I generate a random image in python?",
            "write a python script to generate a random image",
            "create an image gallery component in flutter",
            "fix the image rendering bug in markdown.dart",
            "generate a diagram and save it to docs/arch.svg",
            "build a shader that renders a sunset scene",
            "explain what diffusion models do",
            "/image neon cat",
        ):
            result = intents.classify_image_generation(text)
            self.assertIsNone(result, f"expected None for {text!r}, got {result!r}")


class ImageModelDiscoveryTests(unittest.TestCase):
    def test_identifies_image_models_and_excludes_vision_or_chat(self):
        self.assertTrue(image_generation.is_image_generation_model_name("x/z-image-turbo"))
        self.assertTrue(image_generation.is_image_generation_model_name("x/flux2-klein:latest"))
        self.assertTrue(image_generation.is_image_generation_model_name("sdxl-turbo:latest"))
        self.assertTrue(image_generation.is_image_generation_model_name("stable-diffusion-3.5"))
        self.assertFalse(image_generation.is_image_generation_model_name("qwen3-vl:8b-instruct"))
        self.assertFalse(image_generation.is_image_generation_model_name("llava:13b"))
        self.assertFalse(image_generation.is_image_generation_model_name("moondream:latest"))
        self.assertFalse(image_generation.is_image_generation_model_name("llama-mesh:latest"))
        self.assertFalse(image_generation.is_image_generation_model_name("qwen3-coder:30b"))
        self.assertFalse(image_generation.is_image_generation_model_name("nomic-embed-text"))

    def test_auto_discovers_ollama_image_model_and_calls_generate(self):
        calls: list[tuple[str, str]] = []

        def fake_open_url(req, timeout=30, **_kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            method = req.get_method() if hasattr(req, "get_method") else "GET"
            calls.append((method, url))
            if url.endswith("/api/tags"):
                return _FakeResponse({
                    "models": [
                        {"name": "qwen3-vl:8b-instruct"},
                        {"name": "x/z-image-turbo:latest"},
                        {"name": "qwen3:14b"},
                    ]
                })
            if url.endswith("/api/generate"):
                body = json.loads(req.data.decode("utf-8"))
                self.assertEqual(body["model"], "x/z-image-turbo:latest")
                self.assertEqual(body["stream"], False)
                return _FakeResponse({
                    "model": "x/z-image-turbo:latest",
                    "image": base64.b64encode(_TINY_PNG).decode("ascii"),
                    "done": True,
                })
            raise AssertionError(f"unexpected url: {url}")

        with TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SONDER_IMAGE_API_URL": "", "SONDER_IMAGE_MODEL": ""}, clear=False), \
                 mock.patch.object(image_generation.ollama_endpoint, "open_url", side_effect=fake_open_url):
                result = image_generation.generate_chat_image(
                    "neon cyberpunk fox",
                    state_home=tmp,
                    ollama_endpoints=("http://127.0.0.1:11434",),
                )
            self.assertEqual(result.backend_kind, "ollama")
            self.assertEqual(result.model_id, "x/z-image-turbo:latest")
            self.assertFalse(result.used_fallback)
            self.assertEqual((result.width, result.height), (1, 1))
            self.assertIn("](data:image/png;base64,", result.markdown)
            self.assertTrue(Path(result.saved_path).is_file())
            self.assertEqual(Path(result.saved_path).read_bytes(), _TINY_PNG)
            self.assertEqual([m for m, _ in calls], ["GET", "POST"])

    def test_falls_back_cleanly_to_assetgen_when_no_image_model_installed(self):
        def fake_open_url(req, timeout=30, **_kwargs):
            return _FakeResponse({"models": [{"name": "qwen3-vl:8b-instruct"}, {"name": "sonder:latest"}]})

        with TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SONDER_IMAGE_API_URL": "", "SONDER_IMAGE_MODEL": ""}, clear=False), \
                 mock.patch.object(image_generation.ollama_endpoint, "open_url", side_effect=fake_open_url):
                result = image_generation.generate_chat_image(
                    "a random image",
                    state_home=tmp,
                    ollama_endpoints=("http://127.0.0.1:11434",),
                )
            self.assertEqual(result.backend_kind, "assetgen")
            self.assertEqual(result.model_id, "sonder-assetgen")
            self.assertTrue(result.used_fallback)
            self.assertEqual((result.width, result.height), (512, 320))
            self.assertIn("](data:image/png;base64,", result.markdown)
            self.assertIn("ollama pull x/z-image-turbo", result.markdown)
            saved = Path(result.saved_path)
            self.assertTrue(saved.is_file())
            self.assertTrue(saved.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_uses_default_home_when_state_home_omitted(self):
        def fake_open_url(req, timeout=30, **_kwargs):
            return _FakeResponse({"models": []})

        with TemporaryDirectory() as tmp:
            image_generation.runtime_paths.configure_home(tmp)
            try:
                with mock.patch.dict(os.environ, {"SONDER_IMAGE_API_URL": "", "SONDER_IMAGE_MODEL": ""}, clear=False), \
                     mock.patch.object(image_generation.ollama_endpoint, "open_url", side_effect=fake_open_url):
                    result = image_generation.generate_chat_image(
                        "a random image",
                        ollama_endpoints=("http://127.0.0.1:11434",),
                    )
                self.assertTrue(Path(result.saved_path).is_relative_to(Path(tmp) / "generated-images"))
            finally:
                image_generation.runtime_paths.reset_home()


class StreamChunkingTests(unittest.TestCase):
    def test_large_inline_image_content_is_chunked_into_bounded_sse_lines(self):
        handler = _FakeHandler()

        def fake_chunk(iid, model, delta, **_kwargs):
            return "data: " + json.dumps({
                "id": f"chatcmpl-{iid}",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": delta}],
            }) + "\n\n"

        big_content = "![img](data:image/png;base64," + ("A" * 200_000) + ")"
        ok = live_stream.write_stream_body(
            handler, fake_chunk, "img123", "sonder", big_content, 12, None, None, None,
        )
        self.assertTrue(ok)
        raw = handler.wfile.getvalue().decode("utf-8")
        lines = [line for line in raw.splitlines() if line.startswith("data: ")]
        # Must emit multiple content chunks + terminal frame + [DONE]
        self.assertGreaterEqual(len(lines), 5)
        assembled = []
        for line in lines:
            self.assertLess(len(line), 80_000, "each SSE line must stay well under 1 MiB")
            payload = line[6:]
            if payload == "[DONE]":
                continue
            obj = json.loads(payload)
            delta = obj["choices"][0]["delta"]
            if "content" in delta:
                assembled.append(delta["content"])
        self.assertEqual("".join(assembled), big_content)


class InlineImageContextAccountingTests(unittest.TestCase):
    def test_strip_inline_data_images_replaces_large_base64_payloads(self):
        big_md = (
            "![retro synthwave desert](data:image/png;base64,"
            + ("A" * 600_000)
            + ")\n\nGenerated via `sdxl-turbo` (512x512)."
        )
        cleaned = image_generation.strip_inline_data_images(big_md)
        self.assertNotIn("data:image/", cleaned)
        self.assertIn("[Generated image: retro synthwave desert]", cleaned)
        self.assertIn("Generated via `sdxl-turbo` (512x512).", cleaned)
        self.assertLess(len(cleaned), 200)

    def test_memory_store_estimate_ignores_base64_image_bytes(self):
        from sonder_runtime.adapters import memory_store

        big_reply = "![cyberpunk city](data:image/png;base64," + ("B" * 700_000) + ")\nDone."
        tokens_in, tokens_out = memory_store.estimate_interaction_tokens(
            "generate an image of a cyberpunk city", "", big_reply,
        )
        self.assertLess(tokens_in, 50)
        self.assertLess(tokens_out, 50)

    def test_context_health_ignores_base64_image_bytes(self):
        from sonder_runtime.application.context_health import (
            ContextHealthService,
            ContextHealthSettings,
            ContextMemorySnapshot,
        )

        class _Id:
            def session(self, v): return v or "s1"
            def project(self, v): return v or "default"

        class _Repo:
            def snapshot(self, s, p, limit):
                big = "![lighthouse](data:image/png;base64," + ("C" * 700_000) + ")\nSaved."
                return ContextMemorySnapshot(
                    turns=(("draw a lighthouse", big), ("draw a city", big)),
                    total_turns=2,
                )

        class _Policy:
            def policy(self, req):
                return {"requested": 32768, "native": 32768, "native_max": 32768, "virtual_max": 32768, "mode": "native", "virtual": False}

        class _Metrics:
            def tokens(self, v): return (len(v) + 3) // 4
            def tokens_from_chars(self, c): return (c + 3) // 4
            def bar(self, r): return ""

        svc = ContextHealthService(
            identity=_Id(), repository=_Repo(), policy=_Policy(), metrics=_Metrics(),
            settings=ContextHealthSettings(requested_context=32768, max_turns=20, db_path="", state_home=""),
        )
        snap = svc.snapshot("s1", "default")
        self.assertLess(snap["estimated_tokens"], 100)
        self.assertEqual(snap["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
