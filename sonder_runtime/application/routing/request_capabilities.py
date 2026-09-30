"""Request protocol requirements and availability-preserving evidence policy.

Lexical task labels are not protocol evidence. Unknown features remain unverified
and eligible by default; operators can explicitly require measured passes.
"""
from __future__ import annotations

from collections.abc import Mapping

from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability,
    EvidenceState,
)

LONG_CONTEXT_TOKENS = 8192
CAPABILITY_ROUTING_MODES = frozenset({"advisory", "strict", "off"})


def request_requirements(
    payload: Mapping | None = None, *, prompt: str = "", tools=False,
    structured_output=False, has_image=False, approx_tokens: int = 0,
    long_context=False,
) -> frozenset[BackendCapability]:
    """Recognize Ollama/OpenAI request shapes without inspecting task keywords.

The character estimate is deliberately conservative (UTF-8 bytes / 3); it is
    an admission trigger, not reported token usage. Allocated context capacity
    alone is not a long input: ordinary chat also uses the server's large window.
"""
    if payload is not None and not isinstance(payload, Mapping):
        raise ValueError("request payload must be a mapping")
    value = payload or {}
    options = value.get("options") or {}
    if not isinstance(options, Mapping):
        options = {}
    has_tools = bool(tools or value.get("tools") or options.get("tools"))
    formats = (value.get("format"), options.get("format"))
    response_formats = (value.get("response_format"), options.get("response_format"))
    has_schema = bool(structured_output) or any(
        isinstance(item, Mapping) or item == "json" for item in formats
    ) or any(isinstance(item, Mapping) and item.get("type") != "text"
             for item in response_formats)
    image = bool(has_image or value.get("images") or options.get("images"))
    byte_count = len(str(prompt or value.get("prompt") or "").encode("utf-8"))
    byte_count += len(str(value.get("system") or "").encode("utf-8"))
    for message in value.get("messages", ()) or ():
        if not isinstance(message, Mapping):
            continue
        image = image or bool(message.get("images"))
        content = message.get("content", "")
        if isinstance(content, str):
            byte_count += len(content.encode("utf-8"))
        elif isinstance(content, (list, tuple)):
            for part in content:
                if isinstance(part, Mapping):
                    image = image or part.get("type") in {"image_url", "input_image", "image"}
                    byte_count += len(str(part.get("text", "")).encode("utf-8"))
    counts = (approx_tokens, value.get("approx_tokens", 0))
    large = long_context or byte_count >= LONG_CONTEXT_TOKENS * 3 or any(
        isinstance(count, (int, float)) and count >= LONG_CONTEXT_TOKENS for count in counts
    )
    required = set()
    if has_tools:
        required.add(BackendCapability.TOOL_NATIVE)
    if has_schema:
        required.add(BackendCapability.STRUCTURED)
    if has_tools and has_schema:
        required.add(BackendCapability.TOOLS_WITH_SCHEMA)
    if image:
        required.add(BackendCapability.VISION)
    if large:
        required.add(BackendCapability.LONG_CONTEXT)
    return frozenset(required)


def check_request_evidence(
    evidence, model: str, required, *, backend: str = "ollama", identity=None, now=None,
    mode: str = "advisory",
) -> tuple[bool, str]:
    """Assess eligibility without representing unverified support as measured.

    Advisory rejects only current measured failures of requested capabilities.
    Selection layers retain the configured route if every candidate is rejected.
    Strict requires passes; off never reads the store. Plain text is unchanged.
    """
    if mode not in CAPABILITY_ROUTING_MODES:
        raise ValueError("capability routing must be advisory, strict or off")
    if mode == "off":
        return True, "capability evidence off"
    required = frozenset(BackendCapability(item) for item in required)
    if not required or required == {BackendCapability.CHAT}:
        return True, "plain_text_fallback: capability evidence not required"
    if evidence is None:
        return mode != "strict", "unverified: recent_capability_evidence_missing"
    verdict = evidence.assess(
        model, required, backend=backend, identity=identity, now=now,
    )
    names = ",".join(sorted(item.value for item in required))
    if mode == "strict":
        return verdict.state is EvidenceState.PASSED, f"capabilities [{names}]: {verdict.reason_code}"
    # assess() also checks CHAT for strict role routing. Advisory only excludes
    # failures of explicit request requirements, not that implicit prerequisite.
    states = [verdict.capabilities[item] for item in required]
    failed = EvidenceState.FAILED in states
    unverified = any(state not in {EvidenceState.PASSED, EvidenceState.FAILED} for state in states)
    qualifier = "unverified; " if unverified else ""
    return not failed, f"capabilities [{names}]: {qualifier}{verdict.reason_code}"
