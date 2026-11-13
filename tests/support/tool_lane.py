"""Shared constants and fakes for the tool-lane deny-list tests.

Nothing here touches the model or the network: ``RecordingGateway`` stands in
for the shared packaged gateway and records every request it is asked to serve.
"""
from __future__ import annotations

import json
from dataclasses import replace

DENIED = "astra-cyber-27b"
DENIED_SMALL = "astra-cyber-9b"
ALLOWED = "qwen3.5:4b"
HF_QWEN = "hf.co/mradermacher/Qwen3.8-cyber-RedTeam-Surgical-Abliterated-GGUF:Q4_K_M"
HF_ORNITH = "hf.co/DuoNeural/Cyber-Ornith-1.5-9B-OBLITERATED-GGUF"

BLOCKED = (
    "model '%s' is blocked from tool-using lanes by runtime policy; "
    "it can be used for chat by name only."
)
UNREADABLE = (
    "model '%s' cannot be used in tool-using lanes while the runtime policy "
    "file is unreadable; repair the file (`/runtime status` shows the error) "
    "or reset it with `/runtime reset`."
)
SERVED = (
    "tier '%s' is served by %s with model '%s', which is blocked from "
    "tool-using lanes by runtime policy; it can be used for chat only."
)
UNKNOWN = (
    "tier '%s' is served by %s and the model it serves cannot be determined, "
    "so it cannot be used in tool-using lanes; name the served model for this "
    "tier in that provider's configuration (chat still works)."
)
UNKNOWN_OPENAI = (
    "tier '%s' is served by openai_compatible and the model it serves cannot "
    "be determined, so it cannot be used in tool-using lanes; set "
    "SONDER_OPENAI_MODEL to the model that endpoint serves (chat still works)."
)
# The default route's refusal for a tier it can step up to: (tier, that tier's
# own refusal, tier).
ESCALATION = (
    "the default route can step up to tier '%s' (automatic escalation), and %s "
    "To run this lane, bind tier '%s' to an allowed model or name an allowed "
    "tier for the lane."
)

# Variables that choose a provider, a served model or a tier binding.  The
# repository-level conftest already scrubs ambient SONDER_* variables; this
# list makes each test independent of that and of earlier tests.
PROVIDER_ENV = (
    "SONDER_MODEL_BACKEND", "SONDER_FAST_PROVIDER", "SONDER_GENERAL_PROVIDER",
    "SONDER_CODE_PROVIDER", "SONDER_REASONING_PROVIDER", "SONDER_VISION_PROVIDER",
    "SONDER_EMBEDDING_PROVIDER", "SONDER_INFERENCE_FALLBACK", "SONDER_INFERENCE_MODEL",
    "SONDER_INFERENCE_TIER_MODELS", "SONDER_INFERENCE_BASE_URL",
    "SONDER_INFERENCE_READY_FILE", "SONDER_OPENAI_MODEL", "SONDER_OPENAI_BASE_URL",
    "SONDER_OPENROUTER_MODEL", "SONDER_OPENROUTER_TIER_MODELS", "OPENROUTER_API_KEY",
)
TIER_ENV = (
    "SONDER_FAST", "SONDER_CODE", "SONDER_GENERAL", "SONDER_REASONING",
    "SONDER_VISION", "SONDER_CODE_LOCAL", "SONDER_EMBED_MODEL",
)

RUNNABLE = (
    "Here:\n\n```python\n"
    "def double(x):\n    return x * 2\n\n"
    "assert double(2) == 4\nprint('ok')\n"
    "```\n"
)


def clear_environment(monkeypatch) -> None:
    for name in PROVIDER_ENV + TIER_ENV:
        monkeypatch.delenv(name, raising=False)


def write_policy(path, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")


class RecordingGateway:
    """The shared packaged gateway stand-in: records every request it serves."""

    def __init__(self):
        self.calls = []
        self.routes = []
        self.embeds = []

    def generate(self, request, context):
        from sonder_runtime.application.ports.model_gateway import ModelResponse

        self.calls.append(request)
        return ModelResponse(text="answered", model="inner", tier=request.tier, tokens_out=1)

    def resolve_route(self, request, context):
        self.routes.append(request)
        return "route-sentinel"

    def embed(self, texts, context):
        self.embeds.append(list(texts))
        return ["embedding-sentinel"]

    def other_entry_point(self):  # pragma: no cover - must never be reachable through the wrapper
        raise AssertionError("reached through the wrapper")


def operation_context(**changes):
    from sonder_runtime.application.context import local_owner_context

    return replace(
        local_owner_context(correlation_id="tool-lane-test", source="mcp", timeout_seconds=5),
        **changes,
    )
