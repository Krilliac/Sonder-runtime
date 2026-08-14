import base64

import pytest

import server
import sonder_serve


def _png(tmp_path):
    path = tmp_path / "sample.png"
    # A valid 1x1 PNG.  The vision boundary only needs bytes; decoding remains
    # Ollama's job and is exercised by the live local smoke separately.
    path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL5XQAAAABJRU5ErkJggg=="
    ))
    return path


def test_vision_analysis_sends_guarded_png_to_bound_local_model(monkeypatch, tmp_path):
    image = _png(tmp_path)
    monkeypatch.setitem(server.TIERS, "vision", "qwen2.5vl:3b")
    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: True)
    monkeypatch.setattr(server.context_policy, "native", lambda *_args: 4000)
    monkeypatch.setattr(
        server, "resolve_discovered_model_record",
        lambda _model: ("qwen2.5vl:3b", {"capabilities": ["vision", "completion"]}),
    )
    monkeypatch.setattr(server.file_ops, "require_read_access", lambda *_args, **_kwargs: image)
    monkeypatch.setattr(
        server.workbench, "image_inspect",
        lambda *_args, **_kwargs: {"format": "PNG", "bytes": image.stat().st_size},
    )
    seen = {}

    def chat(payload, **kwargs):
        seen["payload"] = payload
        seen["kwargs"] = kwargs
        return {}, "red"

    monkeypatch.setattr(server, "_chat_request", chat)

    assert server._vision_analyze_impl(str(image), "What color is this?") == "red"
    payload = seen["payload"]
    assert seen["kwargs"] == {
        "model": "qwen2.5vl:3b", "cloud": False, "timeout": server.TIMEOUT,
        "idempotent": True,
    }
    assert payload["model"] == "qwen2.5vl:3b"
    assert payload["options"]["num_ctx"] == 4000
    assert payload["messages"][1]["content"] == "What color is this?"
    assert base64.b64decode(payload["messages"][1]["images"][0]) == image.read_bytes()
    assert "untrusted data" in payload["messages"][0]["content"]


def test_vision_analysis_rejects_remote_endpoint_before_reading_image(monkeypatch, tmp_path):
    image = _png(tmp_path)
    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: False)
    monkeypatch.setattr(
        server.file_ops, "require_read_access",
        lambda *_args, **_kwargs: pytest.fail("image must not be read"),
    )

    with pytest.raises(server.ModelCallError, match="loopback Ollama"):
        server._vision_analyze_impl(str(image), "describe")


def test_vision_analysis_requires_catalog_vision_capability(monkeypatch, tmp_path):
    image = _png(tmp_path)
    monkeypatch.setitem(server.TIERS, "vision", "text-model:latest")
    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: True)
    monkeypatch.setattr(
        server, "resolve_discovered_model_record",
        lambda _model: ("text-model:latest", {"capabilities": ["completion"]}),
    )
    monkeypatch.setattr(
        server.file_ops, "require_read_access",
        lambda *_args, **_kwargs: pytest.fail("image must not be read"),
    )

    with pytest.raises(server.ModelCallError, match="does not declare vision"):
        server._vision_analyze_impl(str(image), "describe")


def test_vision_analysis_refuses_small_operator_context_before_image_read(monkeypatch, tmp_path):
    image = _png(tmp_path)
    monkeypatch.setitem(server.TIERS, "vision", "qwen2.5vl:3b")
    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: True)
    monkeypatch.setattr(server.context_policy, "native", lambda *_args: 2048)
    monkeypatch.setattr(
        server, "resolve_discovered_model_record",
        lambda _model: ("qwen2.5vl:3b", {"capabilities": ["vision"]}),
    )
    monkeypatch.setattr(
        server.file_ops, "require_read_access",
        lambda *_args, **_kwargs: pytest.fail("image must not be read"),
    )

    with pytest.raises(server.ModelCallError, match="contextsize 4k"):
        server._vision_analyze_impl(str(image), "describe")


def test_vision_command_requires_path_and_question(monkeypatch):
    assert server.control_command("/vision") == "usage: /vision <image path> | <question>"
    seen = {}
    monkeypatch.setattr(
        server, "vision_analyze",
        lambda *, path, prompt: seen.update(path=path, prompt=prompt) or "ok",
    )
    assert server.control_command("/vision diagram.png | summarize the chart") == "ok"
    assert seen == {"path": "diagram.png", "prompt": "summarize the chart"}


@pytest.mark.parametrize("command", ["/vision image.png | describe", "/vision_analyze path=x prompt=y"])
def test_served_vision_commands_require_developer_authorization(command):
    assert sonder_serve._dangerous_http_slash(command) is True
