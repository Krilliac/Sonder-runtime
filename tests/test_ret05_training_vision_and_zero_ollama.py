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

    # Verify `show` uses real SI /v1/sonder/identity, while mutating LoRA alias commands
    # (create/cp/rm) fail closed with "not yet supported by Sonder-Inference" and ZERO HTTP calls.
    res_show = artifacts.run_external(
        forbidden_runner,
        [artifacts.ollama_executable(), "show", "sonder-personal:cand"],
        ollama_command=True,
        si_opener=fake_open,
        si_origin="http://127.0.0.1:11437",
    )
    assert res_show.returncode == 0
    assert len(calls) == 1
    assert calls[0][0] == "GET"
    assert "/v1/sonder/identity" in calls[0][1]
    assert artifacts.model_show_status(res_show, "sonder-personal:cand") == "exists"
    assert len(artifacts.show_identity(res_show)) == 64

    def forbidden_http(*a, **k):
        raise AssertionError("mutating LoRA alias op must never hit invented SI HTTP routes")

    res_create = artifacts.run_external(
        forbidden_runner,
        [artifacts.ollama_executable(), "create", "sonder-personal:cand", "-f", str(modelfile)],
        ollama_command=True,
        si_opener=forbidden_http,
        si_origin="http://127.0.0.1:11437",
    )
    assert res_create.returncode != 0
    assert "not yet supported by Sonder-Inference" in res_create.stderr

    res_cp = artifacts.run_external(
        forbidden_runner,
        [artifacts.ollama_executable(), "cp", "sonder-personal:cand", "sonder-personal:live"],
        ollama_command=True,
        si_opener=forbidden_http,
        si_origin="http://127.0.0.1:11437",
    )
    assert res_cp.returncode != 0
    assert "not yet supported by Sonder-Inference" in res_cp.stderr

    res_rm = artifacts.run_external(
        forbidden_runner,
        [artifacts.ollama_executable(), "rm", "sonder-personal:cand"],
        ollama_command=True,
        si_opener=forbidden_http,
        si_origin="http://127.0.0.1:11437",
    )
    assert res_rm.returncode != 0
    assert "not yet supported by Sonder-Inference" in res_rm.stderr


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

    # Invalid format value (neither "json" nor a schema dict) must be refused, not silently dropped.
    bad_fmt_payload = {
        "model": "qwen3.6:35b",
        "messages": [{"role": "user", "content": "hi"}],
        "format": "xml",
    }
    assert provider_bridge.ollama_only_feature(bad_fmt_payload, provider="sonder_inference") is not None
    try:
        provider_bridge.model_request_from_ollama_payload(
            bad_fmt_payload, tier="code", provider="sonder_inference",
        )
    except provider_bridge.UnsupportedProviderFeature:
        pass
    else:
        raise AssertionError("invalid format='xml' must raise UnsupportedProviderFeature")


def test_server_post_and_fanout_translate_openai_chat_completions(monkeypatch):
    """BLOCKING (3): _post and _fanout_synthesis_generate send OpenAI JSON and parse choices[0].message."""
    import server as srv

    sent_bodies = []
    sent_headers = []

    class _FakeResp:
        def __init__(self, body: dict):
            self._raw = json.dumps(body).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self._raw

    def fake_open_url(req, timeout=30):
        sent_bodies.append(json.loads(req.data.decode("utf-8")))
        sent_headers.append(dict(req.header_items()))
        return _FakeResp({
            "id": "chatcmpl-si-1",
            "object": "chat.completion",
            "model": "qwen3.6:35b",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "synthesized answer"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5},
        })

    monkeypatch.setattr(srv.OLLAMA_POOL, "open_url", fake_open_url)
    monkeypatch.setattr(srv, "BASE", "http://127.0.0.1:11437")
    monkeypatch.setattr(srv, "dispatch_provider", lambda provider, path, body, transport: transport())

    out = srv._post(
        "/v1/chat/completions",
        {
            "model": "qwen3.6:35b",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 64, "num_ctx": 8192},
            "keep_alive": "2m",
            "think": False,
        },
        local_only=True,
    )
    assert len(sent_bodies) == 1
    wire = sent_bodies[0]
    assert "options" not in wire
    assert "extra_body" not in wire
    assert "keep_alive" not in wire
    assert "think" not in wire
    assert wire["num_ctx"] == 8192
    assert wire["max_tokens"] == 64
    assert wire["temperature"] == 0.2
    assert wire["chat_template_kwargs"] == {"enable_thinking": False}
    assert out["message"]["content"] == "synthesized answer"
    assert out["done_reason"] == "stop"

    # Also verify _fanout_synthesis_generate handles an OpenAI choices[0].message answer from _post_model.
    monkeypatch.setattr(
        srv,
        "_post_model",
        lambda path, payload, **kwargs: (
            {
                "id": "chatcmpl-fanout",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "merged fanout"}, "finish_reason": "stop"}],
            },
            1,
        ),
    )
    synth = srv._fanout_synthesis_generate(
        "qwen3.6:35b",
        json.dumps({"question": "q", "sources": [{"model": "a", "answer": "x"}]}),
    )
    assert "merged fanout" in synth


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
            ollama_host_allowlist = {
                "sonder_runtime/adapters/inference/ollama_endpoint.py",
                "sonder_runtime/platform/unsafe_lab_policy.py",
                "sonder_runtime/platform/config.py",
                "sonder_runtime/domain/ollama_policy.py",
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    val = node.value
                    if _API_PAT.search(val):
                        offenders.append(f"{rel}:{node.lineno} api={val[:60]!r}")
                    if _PORT_PAT.search(val):
                        offenders.append(f"{rel}:{node.lineno} port11434={val[:60]!r}")
                    if val == "OLLAMA_HOST" and rel not in ollama_host_allowlist:
                        offenders.append(f"{rel}:{node.lineno} env_read={val!r}")
    assert offenders == [], "Remaining local-Ollama literals:\n" + "\n".join(offenders[:30])


def test_scheduled_refresh_and_capabilities_ignore_ollama_host_and_bind_inference_url(tmp_path, monkeypatch):
    """SECURITY (Round 3): OLLAMA_HOST never diverts scheduled cloud refresh or cmd_capabilities."""
    from sonder_runtime import __main__ as cli_main
    from sonder_runtime.adapters import cloud_model_refresh_schedule
    from sonder_runtime.adapters.inference import capability_refresh, ollama_endpoint
    from sonder_runtime.platform.config import InferenceConfig, OllamaConfig, SonderConfig, StateConfig

    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:11437")
    monkeypatch.setenv("SONDER_INFERENCE_API_KEY", "si-secret-token-only-for-si")

    seen_cap_origins = []
    monkeypatch.setattr(
        capability_refresh,
        "refresh_capabilities",
        lambda *, origin, **_kw: seen_cap_origins.append(origin) or {"models": []},
    )
    cfg = SonderConfig(
        state=StateConfig(home=str(tmp_path)),
        inference=InferenceConfig(url="http://127.0.0.1:11437"),
        ollama=OllamaConfig(url="http://127.0.0.1:11434"),
    )
    monkeypatch.setattr(cli_main, "_load_config", lambda _args: cfg)
    rc = cli_main.cmd_capabilities(
        SimpleNamespace(model=None, timeout=1.0, context_tokens=None, json=True)
    )
    assert rc == 0
    assert seen_cap_origins == ["http://127.0.0.1:11437"]
    assert all("11434" not in o for o in seen_cap_origins)

    # Also prove start_scheduled_refresh called with config.inference.url never contacts OLLAMA_HOST.
    seen_urls = []
    seen_auth = []

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return b'{"object":"list","data":[]}'

    def fake_open(req, timeout=5.0, allow_remote=None):
        seen_urls.append(req.full_url)
        seen_auth.append(dict(req.header_items()).get("Authorization"))
        return _Resp()

    monkeypatch.setattr(ollama_endpoint, "open_url", fake_open)
    from sonder_runtime.adapters.inference.ollama_cloud_metadata import OllamaCloudMetadata

    # Verify __main__ passes config.inference.url (never config.ollama.url) at both MCP call sites.
    main_src = (REPO_ROOT / "sonder_runtime" / "__main__.py").read_text(encoding="utf-8")
    assert "start_scheduled_refresh(origin=config.ollama.url)" not in main_src
    assert main_src.count("start_scheduled_refresh(origin=config.inference.url)") == 2
    assert "origin=config.ollama.url" not in main_src

    client = OllamaCloudMetadata(cfg.inference.url, opener=fake_open)
    assert client.tags() == ()
    assert seen_urls == ["http://127.0.0.1:11437/v1/models"]
    assert all("11434" not in u for u in seen_urls)
    assert seen_auth == ["Bearer si-secret-token-only-for-si"]

    # Drive the real scheduled refresh loop with OLLAMA_HOST=11434 set; verify zero requests to :11434.
    import threading
    from sonder_runtime.adapters import cloud_model_refresh

    seen_urls.clear()
    seen_auth.clear()
    monkeypatch.setattr(cloud_model_refresh_schedule, "FIRST_DELAY_SECONDS", 0)
    monkeypatch.setattr(cloud_model_refresh_schedule, "RECHECK_SECONDS", None)
    monkeypatch.setattr(
        cloud_model_refresh,
        "OllamaCloudMetadata",
        lambda origin, **kw: OllamaCloudMetadata(origin, opener=fake_open, **kw),
    )
    monkeypatch.setattr(
        cloud_model_refresh.catalog_client,
        "fetch_catalog",
        lambda *a, **k: cloud_model_refresh.catalog_client.CatalogSnapshot(families={}, refreshed=()),
    )
    captured_loop: dict[str, object] = {}

    class _SyncThread:
        def __init__(self, *, target, kwargs, daemon=True, name=""):
            captured_loop["target"] = target
            captured_loop["kwargs"] = kwargs
        def start(self):
            captured_loop["target"](**captured_loop["kwargs"])

    started = cloud_model_refresh_schedule.start_scheduled_refresh(
        origin=cfg.inference.url,
        thread_factory=_SyncThread,
        home=tmp_path / "sched_home",
        env={
            "SONDER_CLOUD_MODEL_REFRESH": "1",
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "SONDER_INFERENCE_BASE_URL": "http://127.0.0.1:11437",
            "SONDER_INFERENCE_API_KEY": "si-secret-token-only-for-si",
        },
        stop=threading.Event(),
    )
    assert started is True
    assert seen_urls and all(u.startswith("http://127.0.0.1:11437/") for u in seen_urls)
    assert all("11434" not in u for u in seen_urls)
    assert all(a == "Bearer si-secret-token-only-for-si" for a in seen_auth)


def test_reasoning_continuation_and_single_send_enforce_local_only_cloud_fence(monkeypatch):
    """SECURITY (Round 3): reasoning_continuation and single_send on SI rungs always send X-Sonder-Local-Only: 1."""
    import server as srv
    from sonder_runtime.application.chat import provider_bridge

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "0")
    sent_headers = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1):
            return json.dumps({
                "id": "chatcmpl-fence",
                "object": "chat.completion",
                "model": "gpt-oss:120b",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "local answer"},
                    "finish_reason": "stop",
                }],
            }).encode("utf-8")

    def fake_open(req, timeout=30):
        sent_headers.append(dict(req.header_items()))
        return _Resp()

    monkeypatch.setattr(srv.OLLAMA_POOL, "open_url", fake_open)
    monkeypatch.setattr(srv, "BASE", "http://127.0.0.1:11437")
    monkeypatch.setattr(srv, "dispatch_provider", lambda provider, path, body, transport: transport())
    monkeypatch.setattr(srv, "_HOST_MODEL_REQUEST_ADMISSION", SimpleNamespace(try_acquire=lambda: None))

    payload = {
        "model": "gpt-oss:120b",
        "messages": [{"role": "user", "content": "private question"}],
        "stream": False,
        "options": {"num_predict": 256},
    }
    with provider_bridge.bind_rung("sonder_inference", "reasoning"):
        _out, text = srv._chat_request(
            dict(payload),
            model="gpt-oss:120b",
            cloud=False,
            local_only=False,
            reasoning_continuation=True,
            reasoning_total_tokens=512,
        )
    assert text == "local answer"
    assert sent_headers[-1].get("X-sonder-local-only") == "1"

    with provider_bridge.bind_rung("sonder_inference", "fast"):
        _out2, text2 = srv._chat_request(
            {**payload, "think": False},
            model="gpt-oss:120b",
            cloud=False,
            local_only=True,
            single_send=True,
        )
    assert text2 == "local answer"
    assert sent_headers[-1].get("X-sonder-local-only") == "1"

    # Explicitly consented cloud call omits X-Sonder-Local-Only so SI's ollama_cloud backend can serve it.
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    _out3, text3 = srv._chat_request(
        dict(payload),
        model="gpt-oss:120b",
        cloud=True,
        local_only=False,
    )
    assert text3 == "local answer"
    assert "X-sonder-local-only" not in sent_headers[-1]


def test_embeddings_sends_si_bearer_only_to_configured_si_origin_never_dedicated_embed_host(tmp_path, monkeypatch):
    """SECURITY (Round 3): embeddings never sends the SI bearer key to a separate SONDER_EMBED_BASE_URL host."""
    from sonder_runtime.adapters import embeddings
    from sonder_runtime.adapters.inference import ollama_endpoint

    key_file = tmp_path / "si_embed.key"
    key_file.write_text("si-only-secret-key\n", encoding="utf-8")
    monkeypatch.delenv("SONDER_INFERENCE_API_KEY", raising=False)
    monkeypatch.setenv("SONDER_INFERENCE_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:11437")
    monkeypatch.setenv("SONDER_EMBED_BASE_URL", "http://127.0.0.1:11999")
    ollama_endpoint.configure_typed_endpoint(None)

    seen = []

    class _Resp:
        def __init__(self, body: dict):
            self._raw = json.dumps(body).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self._raw

    def fake_open(req, timeout=30, allow_remote=None):
        seen.append((req.full_url, dict(req.header_items())))
        if "/v1/sonder/identity" in req.full_url:
            return _Resp({"backend_identity": {"model_digest": "a" * 64}})
        return _Resp({"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    monkeypatch.setattr(ollama_endpoint, "open_url", fake_open)
    monkeypatch.setattr(embeddings, "_EMBED_CACHE", None, raising=False)

    # 1. Dedicated embedder host (127.0.0.1:11999) MUST NOT receive the SI bearer key.
    with embeddings._admitted_embeddings_response({"model": "nomic-embed-text", "input": "hi"}, "http://127.0.0.1:11999", 5):
        pass
    rev_dedicated = embeddings.serving_model_revision(model="nomic-embed-text", base="http://127.0.0.1:11999")
    assert rev_dedicated.endswith("a" * 64)
    for url, hdrs in seen:
        assert "11999" in url
        assert "Authorization" not in hdrs

    # 2. Configured SI origin (127.0.0.1:11437) DOES receive the api_key_file bearer key.
    seen.clear()
    with embeddings._admitted_embeddings_response({"model": "nomic-embed-text", "input": "hi"}, "http://127.0.0.1:11437", 5):
        pass
    rev_si = embeddings.serving_model_revision(model="nomic-embed-text", base="http://127.0.0.1:11437")
    assert rev_si.endswith("a" * 64)
    assert len(seen) == 2
    for url, hdrs in seen:
        assert "11437" in url
        assert hdrs.get("Authorization") == "Bearer si-only-secret-key"


def test_api_key_file_reaches_legacy_server_post_and_get_and_startup_timeout_wires_lifecycle(tmp_path, monkeypatch):
    """Round 3: api_key_file reaches legacy server._post/_get; [inference].startup_timeout_seconds wires lifecycle; bootstrap_engine uses unified remote opt-in."""
    import server as srv
    from scripts import bootstrap_engine
    from sonder_runtime.adapters.inference import ollama_endpoint
    from sonder_runtime.adapters.web import lifecycle
    from sonder_runtime.platform.config import InferenceConfig, SonderConfig, StateConfig

    key_file = tmp_path / "legacy_si.key"
    key_file.write_text("legacy-si-file-token\n", encoding="utf-8")
    monkeypatch.delenv("SONDER_INFERENCE_API_KEY", raising=False)
    monkeypatch.setenv("SONDER_INFERENCE_API_KEY_FILE", str(key_file))

    seen = []

    class _Resp:
        status = 200
        def __init__(self, body: dict):
            self._raw = json.dumps(body).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self._raw

    def fake_open(req, timeout=30, allow_remote=None):
        seen.append((req.full_url, dict(req.header_items())))
        if req.full_url.endswith("/v1/models"):
            return _Resp({"object": "list", "data": []})
        if req.full_url.endswith("/v1/sonder/health"):
            return _Resp({"status": "ready"})
        return _Resp({
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        })

    monkeypatch.setattr(srv.OLLAMA_POOL, "open_url", fake_open)
    monkeypatch.setattr(srv, "BASE", "http://127.0.0.1:11437")
    monkeypatch.setattr(srv, "dispatch_provider", lambda provider, path, body, transport: transport())

    srv._post("/v1/chat/completions", {"model": "qwen3.8:27b", "messages": [{"role": "user", "content": "hi"}]})
    srv._get("/v1/models")
    assert len(seen) == 2
    assert seen[0][1].get("Authorization") == "Bearer legacy-si-file-token"
    assert seen[1][1].get("Authorization") == "Bearer legacy-si-file-token"

    # [inference].startup_timeout_seconds wires lifecycle probe interval & timeout.
    lifecycle.reset_for_tests()
    try:
        lifecycle.configure(SonderConfig(
            state=StateConfig(home=str(tmp_path / "state")),
            inference=InferenceConfig(startup_timeout_seconds=24),
        ))
        lc = lifecycle.get()
        assert lc._ollama_probe_interval == 6.0
        assert lc._ollama_probe_timeout == 2.0
    finally:
        lifecycle.reset_for_tests()

    # bootstrap_engine uses unified remote opt-in (INFERENCE=0 overrides OLLAMA=1) and api_key_file.
    monkeypatch.setattr(ollama_endpoint, "open_url", fake_open)
    ok_blocked, msg_blocked = bootstrap_engine.ensure_ollama_running(env={
        "SONDER_INFERENCE_BASE_URL": "https://192.0.2.50:8443",
        "SONDER_ALLOW_REMOTE_INFERENCE": "0",
        "SONDER_ALLOW_REMOTE_OLLAMA": "1",
        "SONDER_INFERENCE_API_KEY_FILE": str(key_file),
    })
    assert ok_blocked is False
    assert "blocked" in msg_blocked.lower()

    seen.clear()
    ok_local, _msg_local = bootstrap_engine.ensure_ollama_running(env={
        "SONDER_INFERENCE_BASE_URL": "http://127.0.0.1:11437",
        "SONDER_INFERENCE_API_KEY_FILE": str(key_file),
    })
    assert ok_local is True
    assert seen[0][1].get("Authorization") == "Bearer legacy-si-file-token"


def test_primary_si_bearer_never_sent_to_pool_workers(tmp_path, monkeypatch):
    """SECURITY (Round 4 S1): primary SI bearer goes ONLY to exact configured SI origin, never to pool workers."""
    import server as srv
    import sonder_doctor
    from sonder_runtime.adapters.inference import ollama_endpoint, ollama_pool
    from sonder_runtime.adapters.inference.ollama_cloud_metadata import OllamaCloudMetadata
    from sonder_runtime.platform.config import InferenceConfig, OllamaConfig, SonderConfig, StateConfig

    key_file = tmp_path / "primary_si.key"
    key_file.write_text("primary-si-secret-token\n", encoding="utf-8")
    monkeypatch.delenv("SONDER_INFERENCE_API_KEY", raising=False)
    monkeypatch.setenv("SONDER_INFERENCE_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:11437")
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_INFERENCE", "1")
    ollama_endpoint.configure_typed_endpoint("http://127.0.0.1:11437")

    primary = "http://127.0.0.1:11437"
    loopback_worker = "http://127.0.0.1:11500"
    remote_worker = "https://192.0.2.50:8443"

    seen: list[tuple[str, dict[str, str]]] = []

    class _Resp:
        status = 200
        def __init__(self, body: dict):
            self._raw = json.dumps(body).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self._raw

    def fake_open(req, timeout=5.0, allow_remote=None):
        seen.append((req.full_url, dict(req.header_items())))
        if req.full_url.endswith("/v1/sonder/health"):
            return _Resp({"status": "ready", "version": "1.0.0", "models": [{"id": "m", "state": "loaded"}]})
        if req.full_url.endswith("/v1/models"):
            return _Resp({"object": "list", "data": [{"id": "m"}]})
        return _Resp({
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        })

    try:
        prober = ollama_pool._default_capability_prober(allow_remote=True, open_url=fake_open)
        pool = ollama_pool.OllamaWorkerPool(
            primary,
            (loopback_worker, remote_worker),
            allow_remote=True,
            capability_prober=prober,
        )
        monkeypatch.setattr(pool, "open_url", fake_open)

        # 1. admitted_get: primary gets bearer; loopback worker & remote worker get NONE.
        seen.clear()
        with ollama_pool.admitted_get(pool, primary, "/v1/models", timeout=2.0):
            pass
        with ollama_pool.admitted_get(pool, loopback_worker, "/v1/models", timeout=2.0):
            pass
        with ollama_pool.admitted_get(pool, remote_worker, "/v1/models", timeout=2.0):
            pass
        assert seen[0][1].get("Authorization") == "Bearer primary-si-secret-token"
        assert "Authorization" not in seen[1][1]
        assert "Authorization" not in seen[2][1]

        # 2. capability prober read(): primary gets bearer; loopback worker & remote worker get NONE.
        seen.clear()
        prober(primary)
        prober(loopback_worker)
        prober(remote_worker)
        for url, hdrs in seen:
            if url.startswith(primary + "/"):
                assert hdrs.get("Authorization") == "Bearer primary-si-secret-token"
            else:
                assert "Authorization" not in hdrs, f"leaked primary SI key to worker {url}"

        # 3. server._post send(origin): primary gets bearer; loopback/remote workers get NONE.
        # Pin all three origins deterministically (unpinned pool tie-breaking depends on probe latency),
        # then also exercise one unpinned pool-routed _post asserting strict origin gating.
        pool.refresh_capabilities()
        seen.clear()
        monkeypatch.setattr(srv, "OLLAMA_POOL", pool)
        monkeypatch.setattr(srv, "BASE", primary)
        monkeypatch.setattr(srv, "dispatch_provider", lambda provider, path, body, transport: transport())
        for target_origin in (primary, loopback_worker, remote_worker):
            token = srv._PINNED_OLLAMA_ORIGIN.set(target_origin)
            try:
                srv._post("/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
            finally:
                srv._PINNED_OLLAMA_ORIGIN.reset(token)
        srv._post("/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        post_calls = [(u, h) for u, h in seen if u.endswith("/v1/chat/completions")]
        assert len(post_calls) == 4
        assert post_calls[0][0].startswith(primary + "/") and post_calls[0][1].get("Authorization") == "Bearer primary-si-secret-token"
        assert post_calls[1][0].startswith(loopback_worker + "/") and "Authorization" not in post_calls[1][1]
        assert post_calls[2][0].startswith(remote_worker + "/") and "Authorization" not in post_calls[2][1]
        for url, hdrs in seen:
            if url.startswith(primary + "/"):
                assert hdrs.get("Authorization") == "Bearer primary-si-secret-token"
            else:
                assert "Authorization" not in hdrs, f"_post path leaked primary SI key to worker {url}"

        # 4. sonder_doctor._check_ollama_workers: loopback & remote workers get NONE.
        seen.clear()
        monkeypatch.setattr(ollama_endpoint, "open_url", fake_open)
        cfg = SonderConfig(
            state=StateConfig(home=str(tmp_path)),
            inference=InferenceConfig(url=primary, allow_remote=True),
            ollama=OllamaConfig(url=primary, allow_remote=True, workers=(loopback_worker, remote_worker)),
        )
        doctor_res = sonder_doctor._check_ollama_workers(config=cfg)
        assert doctor_res["status"] == "ok"
        assert len(seen) == 2
        for url, hdrs in seen:
            assert "Authorization" not in hdrs, f"doctor leaked primary SI key to worker {url}"

        # 5. OllamaCloudMetadata defense in depth: non-SI origin gets NONE.
        seen.clear()
        assert OllamaCloudMetadata(loopback_worker, opener=fake_open).tags() == ()
        assert len(seen) == 1 and "Authorization" not in seen[0][1]

        # 6. si_auth_headers rejects userinfo, path, query, fragment, and mismatched port/host,
        # and reads SONDER_INFERENCE_READY_FILE consistently when env=None or env is passed.
        from sonder_runtime.adapters.inference.sonder_inference_gateway import si_auth_headers

        assert si_auth_headers(primary) == {"Authorization": "Bearer primary-si-secret-token"}
        assert si_auth_headers(primary + "/") == {"Authorization": "Bearer primary-si-secret-token"}
        for hostile in (
            loopback_worker,
            remote_worker,
            "http://user@127.0.0.1:11437",
            "http://user:pass@127.0.0.1:11437",
            "http://127.0.0.1:11437/v1",
            "http://127.0.0.1:11437?k=v",
            "http://127.0.0.1:11437#frag",
            "ftp://127.0.0.1:11437",
            "",
        ):
            assert si_auth_headers(hostile) == {}, f"unexpected auth header for {hostile!r}"

        ready_file = tmp_path / "si_ready.json"
        ready_file.write_text(json.dumps({"url": "http://127.0.0.1:11439"}), encoding="utf-8")
        ollama_endpoint.configure_typed_endpoint(None)
        monkeypatch.delenv("SONDER_INFERENCE_BASE_URL", raising=False)
        monkeypatch.setenv("SONDER_INFERENCE_READY_FILE", str(ready_file))
        assert si_auth_headers("http://127.0.0.1:11439") == {"Authorization": "Bearer primary-si-secret-token"}
        assert si_auth_headers(primary) == {}
        assert si_auth_headers(
            "http://127.0.0.1:11439",
            env={"SONDER_INFERENCE_READY_FILE": str(ready_file), "SONDER_INFERENCE_API_KEY_FILE": str(key_file)},
        ) == {"Authorization": "Bearer primary-si-secret-token"}
    finally:
        ollama_endpoint.configure_typed_endpoint(None)


def test_inference_allow_remote_satisfies_worker_and_ollama_url_gates(tmp_path):
    """Round 4 (R2): [inference].allow_remote = true satisfies [ollama].workers and [ollama].url gates."""
    from sonder_runtime.platform.config import ConfigError, load_config

    workers_toml = tmp_path / "workers.toml"
    workers_toml.write_text(
        "[inference]\n"
        "allow_remote = true\n"
        "[ollama]\n"
        'workers = ["https://192.0.2.50:443"]\n',
        encoding="utf-8",
    )
    cfg = load_config(workers_toml, env={})
    assert cfg.inference.allow_remote is True
    assert cfg.ollama.workers == ("https://192.0.2.50:443",)

    url_toml = tmp_path / "url.toml"
    url_toml.write_text(
        "[inference]\n"
        "allow_remote = true\n"
        "[ollama]\n"
        'url = "https://192.0.2.50:443"\n',
        encoding="utf-8",
    )
    cfg2 = load_config(url_toml, env={})
    assert cfg2.ollama.url == "https://192.0.2.50:443"

    refused_toml = tmp_path / "refused.toml"
    refused_toml.write_text(
        "[inference]\n"
        "allow_remote = false\n"
        "[ollama]\n"
        'workers = ["https://192.0.2.50:443"]\n',
        encoding="utf-8",
    )
    try:
        load_config(refused_toml, env={})
    except ConfigError as exc:
        assert "remote-Ollama consent gate" in str(exc)
    else:
        raise AssertionError("remote workers without allow_remote must fail closed")

    # Worker duplicating [inference].url is also rejected as a primary duplicate.
    dup_inf_toml = tmp_path / "dup_inf.toml"
    dup_inf_toml.write_text(
        "[inference]\n"
        'url = "http://127.0.0.1:11437"\n'
        "[ollama]\n"
        'workers = ["http://127.0.0.1:11437"]\n',
        encoding="utf-8",
    )
    try:
        load_config(dup_inf_toml, env={})
    except ConfigError as exc:
        assert "duplicates primary" in str(exc)
    else:
        raise AssertionError("worker duplicating [inference].url must fail validation")


def test_ollama_conformance_and_headless_use_origin_gated_api_key_file_and_local_only(tmp_path, monkeypatch):
    """Round 4 (R3): ollama_conformance and sonder_headless honor api_key_file via origin-gated helper; conformance sends X-Sonder-Local-Only."""
    import sonder_headless
    from sonder_runtime.adapters.inference import ollama_conformance, ollama_endpoint

    key_file = tmp_path / "r3_si.key"
    key_file.write_text("r3-file-secret-key\n", encoding="utf-8")
    monkeypatch.delenv("SONDER_INFERENCE_API_KEY", raising=False)
    monkeypatch.setenv("SONDER_INFERENCE_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:11437")
    ollama_endpoint.configure_typed_endpoint("http://127.0.0.1:11437")

    seen: list[tuple[str, dict[str, str]]] = []

    class _Resp:
        status = 200
        def __init__(self, body: dict):
            self._raw = json.dumps(body).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self._raw

    def fake_open(req, timeout=5.0, allow_remote=None):
        seen.append((req.full_url, dict(req.header_items())))
        if req.full_url.endswith("/v1/sonder/health"):
            return _Resp({"status": "ready"})
        if req.full_url.endswith("/v1/models"):
            return _Resp({"object": "list", "data": [{"id": "qwen3.8:27b"}]})
        return _Resp({
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        })

    try:
        monkeypatch.setattr(ollama_conformance, "open_url", fake_open)
        monkeypatch.setattr(ollama_endpoint, "open_url", fake_open)

        # 1. Conformance probe against configured SI origin: sends api_key_file bearer AND X-Sonder-Local-Only: 1.
        probe_si = ollama_conformance.OllamaConformanceProbe("http://127.0.0.1:11437", "qwen3.8:27b")
        probe_si._deadline = 9e18
        seen.clear()
        probe_si._request(
            "/v1/chat/completions",
            {"model": "qwen3.8:27b", "messages": [{"role": "user", "content": "hi"}]},
            timeout=5.0,
        )
        assert len(seen) == 1
        assert seen[0][1].get("Authorization") == "Bearer r3-file-secret-key"
        assert seen[0][1].get("X-sonder-local-only") == "1"

        # 2. Conformance probe against a different loopback port: NO Authorization, still X-Sonder-Local-Only: 1.
        probe_other = ollama_conformance.OllamaConformanceProbe("http://127.0.0.1:11500", "qwen3.8:27b")
        probe_other._deadline = 9e18
        seen.clear()
        probe_other._request(
            "/v1/chat/completions",
            {"model": "qwen3.8:27b", "messages": [{"role": "user", "content": "hi"}]},
            timeout=5.0,
        )
        assert len(seen) == 1
        assert "Authorization" not in seen[0][1]
        assert seen[0][1].get("X-sonder-local-only") == "1"

        # 3. sonder_headless ollama_models() and ollama_ok(): both honor api_key_file via origin-gated helper.
        seen.clear()
        assert sonder_headless.ollama_models() == {"qwen3.8:27b"}
        assert sonder_headless.ollama_ok() is True
        assert len(seen) == 2
        assert seen[0][1].get("Authorization") == "Bearer r3-file-secret-key"
        assert seen[1][1].get("Authorization") == "Bearer r3-file-secret-key"
    finally:
        ollama_endpoint.configure_typed_endpoint(None)


def test_cmd_serve_and_cmd_mcp_accept_runbook_inference_allow_remote_with_https_worker(tmp_path, monkeypatch):
    """Round 5 (R2): runbook TOML ([inference].allow_remote=true + https [ollama].workers) succeeds through cmd_serve and cmd_mcp configure_typed_workers."""
    from sonder_runtime import __main__ as cli_main
    from sonder_runtime.adapters.inference import ollama_endpoint, ollama_pool
    from sonder_runtime.adapters.persistence.sqlite import bridge_migration
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.domain.common.errors import MigrationRequired
    from sonder_runtime.platform import config as sonder_config

    runbook_toml = tmp_path / "runbook.toml"
    runbook_toml.write_text(
        "[state]\n"
        f'home = "{(tmp_path / "home").as_posix()}"\n'
        "[inference]\n"
        "allow_remote = true\n"
        "[ollama]\n"
        'workers = ["https://192.0.2.50:443"]\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("SONDER_ALLOW_REMOTE_INFERENCE", raising=False)
    monkeypatch.delenv("SONDER_ALLOW_REMOTE_OLLAMA", raising=False)
    monkeypatch.delenv("SONDER_OLLAMA_WORKERS", raising=False)

    cfg = sonder_config.load_config(runbook_toml, env={})
    assert cfg.inference.allow_remote is True
    assert cfg.ollama.allow_remote is False
    assert cfg.ollama.workers == ("https://192.0.2.50:443",)

    monkeypatch.setattr(cli_main, "_load_config", lambda _args: cfg)
    monkeypatch.setattr(bridge_migration, "stamp_fresh_home", lambda _home: None)

    def _stop_after_serve_workers(_home):
        raise MigrationRequired("stop-after-worker-config")

    monkeypatch.setattr(bridge_migration, "require_epoch_2", _stop_after_serve_workers)

    serve_args = SimpleNamespace(
        config=str(runbook_toml),
        secrets=None,
        set=None,
        skip_preflight=True,
        skip_ollama=True,
        json=False,
    )
    try:
        rc = cli_main.cmd_serve(serve_args)
        assert rc == 1  # reached require_epoch_2 AFTER configure_typed_workers succeeded
        assert ollama_pool._configured_allow_remote is True
        assert ollama_pool._configured_workers == ("https://192.0.2.50:443",)
    finally:
        ollama_pool.reset_typed_workers()
        ollama_endpoint.reset_typed_endpoint()

    # Also drive cmd_mcp legacy configuration callback through configure_typed_workers.
    class _StopAfterMcpWorkers(RuntimeError):
        pass

    monkeypatch.setattr(cli_main, "_stamp_home_before_stores", lambda *, gate=False: True)
    monkeypatch.setattr(
        bootstrap_app,
        "default_app",
        lambda **_kw: (_ for _ in ()).throw(_StopAfterMcpWorkers()),
    )
    monkeypatch.setattr(cli_main.McpCommand, "execute", lambda self, cb: cb())
    mcp_args = SimpleNamespace(
        config=str(runbook_toml),
        secrets=None,
        set=None,
        native=False,
        legacy=True,
        progressive_tools=False,
        json=False,
    )
    try:
        try:
            cli_main.cmd_mcp(mcp_args)
        except _StopAfterMcpWorkers:
            pass
        else:
            raise AssertionError("expected _StopAfterMcpWorkers after configure_typed_workers")
        assert ollama_pool._configured_allow_remote is True
        assert ollama_pool._configured_workers == ("https://192.0.2.50:443",)
    finally:
        ollama_pool.reset_typed_workers()
        ollama_endpoint.reset_typed_endpoint()

    # cmd_models also binds ollama_endpoint to a TOML-only non-default [inference].url so si_auth_headers attaches the key.
    from sonder_runtime.adapters.inference.sonder_inference_gateway import si_auth_headers
    from sonder_runtime.bootstrap import cloud_models_cli

    custom_toml = tmp_path / "custom_inf.toml"
    custom_toml.write_text(
        "[state]\n"
        f'home = "{(tmp_path / "models_home").as_posix()}"\n'
        "[inference]\n"
        'url = "http://127.0.0.1:11441"\n',
        encoding="utf-8",
    )
    custom_cfg = sonder_config.load_config(custom_toml, env={})
    monkeypatch.delenv("SONDER_INFERENCE_BASE_URL", raising=False)
    monkeypatch.setenv("SONDER_INFERENCE_API_KEY", "toml-only-si-key")
    monkeypatch.setattr(cli_main, "_load_config", lambda _args: custom_cfg)
    captured_auth: list[dict[str, str]] = []
    monkeypatch.setattr(
        cloud_models_cli,
        "run_cloud_refresh",
        lambda _args, *, origin, home=None: captured_auth.append(si_auth_headers(origin)) or 0,
    )
    try:
        rc_models = cli_main.cmd_models(SimpleNamespace(config=str(custom_toml), secrets=None, set=None, json=True))
        assert rc_models == 0
        assert captured_auth == [{"Authorization": "Bearer toml-only-si-key"}]
    finally:
        ollama_endpoint.reset_typed_endpoint()
