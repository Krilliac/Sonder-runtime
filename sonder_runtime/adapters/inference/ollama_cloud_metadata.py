"""Metadata-only Ollama client for cloud model stubs: ``/api/tags``, ``/api/show``, ``/api/pull``.

There is deliberately no chat, generate or embed path here: nothing this
client sends is a prompt, so it needs no cloud opt-in.  Every request goes
through :func:`ollama_endpoint.open_url` with ``allow_remote=None``, so the
configured remote-Ollama policy applies unchanged (no proxies, no redirects).

``show`` turns any HTTP-level answer into a :class:`ProbeResult` through the
pure classifier; only socket/timeout failures, policy refusals and malformed
200 bodies raise :class:`OllamaTransportError`.  A pull is refused before
any I/O unless the name passes the strict cloud stub grammar.

Instances hold only configuration and are safe to share between threads.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint
from sonder_runtime.domain.cloud_model_refresh import (
    ProbeResult,
    ProbeStatus,
    classify_show,
    is_cloud_stub_name,
    is_pulled_cloud_row,
    valid_model_name,
)

MAX_BODY = 1024 * 1024
MAX_ERROR_BODY = 64 * 1024
MAX_TAG_ROWS = 4096

Opener = Callable[..., Any]


class OllamaTransportError(RuntimeError):
    """Ollama could not be reached or answered unusably; ``reason`` is a bounded code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OllamaPullFailed(RuntimeError):
    """Ollama answered a pull with an error; ``reason`` is a bounded code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _decode_json(raw: bytes) -> object:
    return json.loads(raw.decode("utf-8"))


def _error_payload(exc: urllib.error.HTTPError) -> object:
    try:
        raw = exc.read(MAX_ERROR_BODY)
    except (OSError, http.client.HTTPException):
        return None
    try:
        return _decode_json(raw)
    except (ValueError, UnicodeDecodeError):
        text = raw.decode("utf-8", errors="replace").strip()
        return {"error": text} if text else None


class OllamaCloudMetadata:
    """Bounded metadata requests to one Ollama origin; immutable after construction."""

    def __init__(
        self,
        origin: str,
        *,
        opener: Opener = ollama_endpoint.open_url,
        show_timeout: float = 10.0,
        tags_timeout: float = 5.0,
        pull_timeout: float = 300.0,
    ) -> None:
        self._origin = str(origin or "").rstrip("/")
        self._opener = opener
        self._show_timeout = show_timeout
        self._tags_timeout = tags_timeout
        self._pull_timeout = pull_timeout

    def _request(self, path: str, body: dict[str, Any] | None) -> urllib.request.Request:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json"}
        return urllib.request.Request(
            self._origin + path, data=data, headers=headers,
            method="GET" if body is None else "POST",
        )

    def _open(self, request: urllib.request.Request, timeout: float) -> tuple[int, object]:
        """``(status, json)`` for a 2xx answer; HTTPError propagates to the caller."""
        try:
            with self._opener(request, timeout=timeout, allow_remote=None) as response:
                raw = response.read(MAX_BODY + 1)
                status = int(getattr(response, "status", None) or response.getcode() or 200)
        except urllib.error.HTTPError:
            raise
        except ValueError as exc:  # the configured remote policy refused the origin
            raise OllamaTransportError("ollama_policy_refused") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise OllamaTransportError("ollama_unreachable") from exc
        if len(raw) > MAX_BODY:
            raise OllamaTransportError("ollama_body_too_large")
        try:
            return status, _decode_json(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise OllamaTransportError("ollama_malformed_body") from exc

    def tags(self) -> tuple[dict[str, Any], ...]:
        """Pulled cloud stubs as ``{"name", "remote_host"}`` rows (by name or ``remote_host``)."""
        try:
            _status, payload = self._open(self._request("/api/tags", None), self._tags_timeout)
        except urllib.error.HTTPError as exc:
            raise OllamaTransportError("ollama_tags_http_%d" % exc.code) from exc
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise OllamaTransportError("ollama_malformed_body")
        stubs: list[dict[str, Any]] = []
        for row in rows[:MAX_TAG_ROWS]:
            if isinstance(row, dict) and is_pulled_cloud_row(row):
                stubs.append({
                    "name": str(row.get("name") or row.get("model")),
                    "remote_host": bool(row.get("remote_host")),
                })
        return tuple(stubs)

    def show(self, model: str) -> ProbeResult:
        """Probe one model; HTTP errors become a classified result, transport errors raise."""
        if not valid_model_name(model):
            return ProbeResult(str(model)[:120], ProbeStatus.ERROR, detail="invalid_name")
        request = self._request("/api/show", {"model": model, "name": model})
        try:
            status, payload = self._open(request, self._show_timeout)
        except urllib.error.HTTPError as exc:
            return classify_show(model, exc.code, _error_payload(exc))
        return classify_show(model, status, payload)

    def pull_stub(self, model: str) -> None:
        """Pull one cloud stub (``stream: false``); refuses any other name before I/O."""
        if not isinstance(model, str) or not is_cloud_stub_name(model):
            raise ValueError("only cloud stub names (<family>:cloud or :<tag>-cloud) are pulled")
        request = self._request("/api/pull", {"model": model, "stream": False})
        try:
            _status, payload = self._open(request, self._pull_timeout)
        except urllib.error.HTTPError as exc:
            raise OllamaPullFailed("pull_http_%d" % exc.code) from exc
        if not isinstance(payload, dict) or "error" in payload:
            raise OllamaPullFailed("pull_error")


__all__ = [
    "MAX_BODY",
    "OllamaCloudMetadata",
    "OllamaPullFailed",
    "OllamaTransportError",
]
