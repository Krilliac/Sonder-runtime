"""RET-05 Subsystem 6 & 7 tests: training SI LoRA seam, SI vision, bridge, and zero local-Ollama grep."""
from __future__ import annotations

import ast
import json
import pathlib
import re
from types import SimpleNamespace

from sonder_runtime.adapters.inference.ollama_vision import OllamaVisionGateway
from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.adapters.training import alias_lifecycle, artifacts
from sonder_runtime.application.chat import provider_bridge
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.vision_gateway import VisionRequest
from sonder_runtime.application.prefix_cache_report import prewarm_request

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_API_PAT = re.compile(r"/api/(?:chat|generate|tags|ps|show|pull|create|embeddings|embed|version)\b")
_PORT_PAT = re.compile(r"\b11434\b")


def test_training_alias_lifecycle_uses_si_namespace_and_lora_api_without_ollama_subprocess(tmp_path):
    paths = alias_lifecycle.shared_alias_paths(
        local_ollama_origin_fn=lambda: "http://127.0.0.1:11437",
    )
    assert paths["origin"] == "loopback:11437"
    assert "si-" in paths["lock"].name
    assert "11434" not in str(paths["origin"])

    # Default port must be 11437, not 11434.
    default_paths = alias_lifecycle.shared_alias_paths(
        local_ollama_origin_fn=lambda: "http://127.0.0.1",
    )
    assert default_paths["origin"] == "loopback:11437"

    # Verify SI LoRA alias client executes create/show/cp/rm over HTTP without spawning ollama.
    calls = []

    class _FakeResp:
        def __init__(self, body: dict, status: int = 200):
            self._raw = json.dumps(body).encode("utf-8")
            self.status = status

        def read(self, n=-1):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=30.0):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        calls.append((req.get_method(), req.full_url, body))
        if "/v1/sonder/identity" in req.full_url:
            return _FakeResp({
                "schema": "sonder.inference.identity/1",
                "backend_identity": {"model_digest": "b" * 64},
            })
        return _FakeResp({"status": "ok", "alias": "sonder-personal:latest"})

    modelfile = tmp_path / "Modelfile.personal"
    adapter = tmp_path / "adapter.gguf"
    adapter.write_bytes(b"GGUF")
    modelfile.write_text(f"FROM qwen2.5-coder:7b\nADAPTER {adapter}\n", encoding="utf-8")

    def forbidden_runner(*a, **k):
        raise AssertionError("subprocess runner must never be invoked for SI LoRA alias ops")

    res_create = artifacts.run_external(
        forbidden_runner,
        [artifacts.ollama_executable(), "create", "sonder-personal:cand", "-f", str(modelfile)],
        ollama_command=True,
        si_opener=fake_open,
        si_origin="http://127.0.0.1:11437",
    )
    assert res_create.returncode == 0
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/v1/sonder/lora/aliases")
    assert calls[0][2]["alias"] == "sonder-personal:cand"
    assert calls[0][2]["base_model"] == "qwen2.5-coder:7b"
    assert calls[0][2]["adapters"][0]["path"] == str(adapter)

    res_show = artifacts.run_external(
        forbidden_runner,
        [artifacts.ollama_executable(), "show", "sonder-personal:cand"],
        ollama_command=True,
        si_opener=fake_open,
        si_origin="http://127.0.0.1:11437",
    )
    assert res_show.returncode == 0
    assert artifacts.model_show_status(res_show, "sonder-personal:cand") == "exists"
    assert len(artifacts.show_identity(res_show)) == 64

    res_rm = artifacts.run_external(
        forbidden_runner,
        [artifacts.ollama_executable(), "rm", "sonder-personal:cand"],
        ollama_command=True,
        si_opener=fake_open,
        si_origin="http://127.0.0.1:11437",
    )
    assert res_rm.returncode == 0
    assert any(m == "DELETE" and "/v1/sonder/lora/aliases/" in u for m, u, _ in calls)


def test_vision_gateway_sends_openai_image_url_to_si_chat_completions(monkeypatch):
    seen = []

    def fake_transport(url, payload, timeout):
        seen.append((url, payload, timeout))
        return {
            "id": "chatcmpl-vis",
            "model": "qwen3-vl:8b",
            "choices": [{"message": {"role": "assistant", "content": "a red square"}}],
        }

    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:11437")
    gw = OllamaVisionGateway(
        target_resolver=lambda tier, strict: SimpleNamespace(model="qwen3-vl:8b", cloud=False),
        transport=fake_transport,
        provider_bindings=ProviderBindings.uniform("sonder_inference"),
    )
    ctx = local_owner_context(correlation_id="vis-1", cloud_allowed=False)
    req = VisionRequest(
        prompt="describe", image=b"\x89PNG\r\n\x1a\nfake", media_type="image/png", tier="vision",
    )
    resp = gw.analyze(req, ctx)
    assert resp.text == "a red square"
    assert len(seen) == 1
    assert seen[0][0].endswith("/v1/chat/completions")
    user_msg = seen[0][1]["messages"][-1]
    assert isinstance(user_msg["content"], list)
    assert any(part.get("type") == "image_url" for part in user_msg["content"])


def test_provider_bridge_carries_schema_format_and_images_for_sonder_inference():
    import base64
    from sonder_runtime.adapters.inference.sonder_inference_gateway import (
        SonderInferenceConfig,
        SonderInferenceGateway,
    )
    from sonder_runtime.domain.common.errors import InvalidInput

    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")
    jpeg_b64 = base64.b64encode(b"\xff\xd8\xff\xe0fake").decode("ascii")
    payload = {
        "model": "qwen3.6:35b",
        "messages": [
            {"role": "user", "content": "earlier photo", "images": [jpeg_b64]},
            {"role": "assistant", "content": "looks like a cat"},
            {"role": "user", "content": "what is in this picture?", "images": [png_b64]},
        ],
        "format": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    }
    assert provider_bridge.ollama_only_feature(payload, provider="sonder_inference") is None
    req = provider_bridge.model_request_from_ollama_payload(
        payload, tier="code", provider="sonder_inference",
    )
    assert req.options.get("response_format") == {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}},
    }
    assert req.options.get("images") == [png_b64]

    # End-to-end through SonderInferenceGateway: verify both response_format and OpenAI image_url reach POST body.
    posted = []

    def fake_get(url, headers, timeout):
        return (
            200,
            json.dumps({
                "status": "ready",
                "api_version": 1,
                "backends": [{
                    "name": "llamacpp",
                    "available": True,
                    "capabilities": ["chat", "streaming", "structured_output", "vision"],
                }],
                "models": [{
                    "id": "qwen3.6:35b",
                    "backend": "llamacpp",
                    "capabilities": ["chat", "streaming", "structured_output", "vision"],
                }],
            }).encode("utf-8"),
        )

    def fake_post(url, body, headers, timeout):
        posted.append((url, body, headers))
        return {
            "id": "chatcmpl-img",
            "model": "qwen3.6:35b",
            "choices": [{"message": {"role": "assistant", "content": '{"ok":true}'}, "finish_reason": "stop"}],
        }

    gw = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:11437", model="qwen3.6:35b"),
        transport=fake_post,
        get_transport=fake_get,
    )
    ctx = local_owner_context(correlation_id="img-e2e", cloud_allowed=False)
    resp = gw.generate(req, ctx)
    assert resp.text == '{"ok":true}'
    assert len(posted) == 1
    wire = posted[0][1]
    assert "images" not in wire
    assert wire["response_format"]["type"] == "json_schema"
    # History user turn carries JPEG data URI
    hist_content = wire["messages"][0]["content"]
    assert isinstance(hist_content, list)
    assert any(
        p.get("type") == "image_url"
        and p["image_url"]["url"] == f"data:image/jpeg;base64,{jpeg_b64}"
        for p in hist_content
    )
    # Final user turn carries PNG data URI
    last_content = wire["messages"][-1]["content"]
    assert isinstance(last_content, list)
    assert last_content[0] == {"type": "text", "text": "what is in this picture?"}
    assert last_content[1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{png_b64}"},
    }

    # Hostile image data URI (text/html) fails closed before any HTTP POST.
    bad_payload = {
        "model": "qwen3.6:35b",
        "messages": [{"role": "user", "content": "x", "images": ["data:text/html;base64,AAAA"]}],
    }
    bad_req = provider_bridge.model_request_from_ollama_payload(
        bad_payload, tier="code", provider="sonder_inference",
    )
    try:
        gw.generate(bad_req, ctx)
    except InvalidInput:
        pass
    else:
        raise AssertionError("text/html image data URI must fail closed with InvalidInput")
    assert len(posted) == 1


def test_prefix_cache_probe_uses_si_chat_completions():
    path, _body = prewarm_request("qwen3.6:35b", "2m", lambda: ("sys", {}))
    assert path == "/v1/chat/completions"


def test_hostile_inputs_rejected_fail_closed(tmp_path):
    from sonder_runtime.domain.common.errors import Forbidden
    from sonder_runtime.platform import config as sonder_config

    # Traversal in LoRA alias name and adapter path rejected before HTTP I/O.
    bad_alias = artifacts.run_external(
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess forbidden")),
        [artifacts.ollama_executable(), "rm", "../escape"],
        ollama_command=True,
        si_opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP forbidden")),
    )
    assert bad_alias.returncode == 2

    bad_modelfile = tmp_path / "Modelfile.bad"
    bad_modelfile.write_text("FROM base\nADAPTER /tmp/../escape.gguf\n", encoding="utf-8")
    bad_create = artifacts.run_external(
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess forbidden")),
        [artifacts.ollama_executable(), "create", "cand:latest", "-f", str(bad_modelfile)],
        ollama_command=True,
        si_opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP forbidden")),
    )
    assert bad_create.returncode == 1
    assert "traversal" in bad_create.stderr

    # Negative health_ttl_seconds rejected by [inference] config validation.
    bad_toml = tmp_path / "bad_inf.toml"
    bad_toml.write_text("[inference]\nhealth_ttl_seconds = -5.0\n", encoding="utf-8")
    try:
        sonder_config.load_config(bad_toml, env={})
    except sonder_config.ConfigError as exc:
        assert "health_ttl_seconds" in str(exc)
    else:
        raise AssertionError("negative health_ttl_seconds must fail validation")

    # Empty image, injected media_type, and oversized image rejected by VisionRequest AND defense-in-depth in gateway.
    for bad_kwargs in (
        {"prompt": "x", "image": b"", "media_type": "image/png", "tier": "vision"},
        {"prompt": "x", "image": b"abc", "media_type": "image/png;evil=1", "tier": "vision"},
        {"prompt": "x", "image": b"a" * (16 * 1024 * 1024 + 1), "media_type": "image/png", "tier": "vision"},
    ):
        try:
            VisionRequest(**bad_kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"VisionRequest must reject {bad_kwargs!r}")

    gw = OllamaVisionGateway(
        target_resolver=lambda tier, strict: SimpleNamespace(model="qwen3-vl:8b", cloud=False),
        transport=lambda *a, **k: (_ for _ in ()).throw(AssertionError("transport forbidden")),
        provider_bindings=ProviderBindings.uniform("sonder_inference"),
    )
    ctx = local_owner_context(correlation_id="vis-bad", cloud_allowed=False)
    for bad_duck in (
        SimpleNamespace(prompt="x", image=b"", media_type="image/png", tier="vision", options={}),
        SimpleNamespace(prompt="x", image=b"abc", media_type="image/png;evil=1", tier="vision", options={}),
    ):
        try:
            gw.analyze(bad_duck, ctx)
        except Forbidden:
            pass
        else:
            raise AssertionError("hostile vision request must raise Forbidden in gateway")


def test_zero_local_ollama_api_or_port_11434_literals_in_code():
    """Verify no non-docstring local-Ollama /api/* or 11434 literals remain in sonder_runtime/ or runtime/."""
    offenders = []
    for top in ("sonder_runtime", "runtime"):
        for path in sorted((REPO_ROOT / top).rglob("*.py")):
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(path))
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    if (
                        node.body
                        and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)
                    ):
                        docstrings.add(id(node.body[0].value))
            rel = path.relative_to(REPO_ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    val = node.value
                    if _API_PAT.search(val):
                        offenders.append(f"{rel}:{node.lineno} api={val[:60]!r}")
                    if _PORT_PAT.search(val):
                        offenders.append(f"{rel}:{node.lineno} port11434={val[:60]!r}")
    assert offenders == [], "Remaining local-Ollama literals:\n" + "\n".join(offenders[:30])
