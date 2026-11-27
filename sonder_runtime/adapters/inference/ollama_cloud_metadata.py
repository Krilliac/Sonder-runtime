"""Metadata-only Sonder Inference client for Ollama Cloud models via ``/v1/models``.

Local Ollama ``/api/tags``, ``/api/show`` and ``/api/pull`` stub pulls are
retired; Ollama Cloud models are served directly by Sonder Inference's
``ollama_cloud`` backend (RET-02). Nothing this client sends is a prompt.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint
from sonder_runtime.domain.cloud_model_refresh import (
    ProbeResult,
    ProbeStatus,
    is_pulled_cloud_row,
    valid_model_name,
)

MAX_BODY = 1024 * 1024
MAX_ERROR_BODY = 64 * 1024
MAX_TAG_ROWS = 4096

Opener = Callable[..., Any]


class OllamaTransportError(RuntimeError):
    """Sonder Inference could not be reached or answered unusably; ``reason`` is a bounded code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OllamaPullFailed(RuntimeError):
    """Cloud stub pull failed or is retired; ``reason`` is a bounded code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _decode_json(raw: bytes) -> object:
    return json.loads(raw.decode("utf-8"))


class OllamaCloudMetadata:
    """Bounded metadata requests to Sonder Inference ``/v1/models``; immutable after construction."""

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
        headers: dict[str, str] = {} if body is None else {"Content-Type": "application/json"}
        api_key = os.environ.get("SONDER_INFERENCE_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
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
        except ValueError as exc:
            raise OllamaTransportError("inference_policy_refused") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise OllamaTransportError("inference_unreachable") from exc
        if len(raw) > MAX_BODY:
            raise OllamaTransportError("inference_body_too_large")
        try:
            return status, _decode_json(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise OllamaTransportError("inference_malformed_body") from exc

    def _model_rows(self, timeout: float) -> list[dict[str, Any]]:
        try:
            _status, payload = self._open(self._request("/v1/models", None), timeout)
        except urllib.error.HTTPError as exc:
            raise OllamaTransportError("inference_models_http_%d" % exc.code) from exc
        rows = (payload.get("data") if isinstance(payload, dict) else None)
        if not isinstance(rows, list):
            raise OllamaTransportError("inference_malformed_body")
        return [row for row in rows[:MAX_TAG_ROWS] if isinstance(row, dict)]

    def tags(self) -> tuple[dict[str, Any], ...]:
        """Cloud models advertised by Sonder Inference ``/v1/models``."""
        stubs: list[dict[str, Any]] = []
        for row in self._model_rows(self._tags_timeout):
            if is_pulled_cloud_row(row):
                name = str(row.get("id") or row.get("name") or row.get("model") or "")
                raw_sonder = row.get("sonder")
                sonder_meta: dict[str, Any] = raw_sonder if isinstance(raw_sonder, dict) else {}
                remote = bool(
                    row.get("remote_host")
                    or row.get("remote")
                    or sonder_meta.get("remote")
                    or str(sonder_meta.get("provider") or "").strip().lower() == "ollama_cloud"
                )
                stubs.append({"name": name, "remote_host": remote})
        return tuple(stubs)

    def show(self, model: str) -> ProbeResult:
        """Check whether ``model`` is currently served by Sonder Inference ``/v1/models``."""
        if not valid_model_name(model):
            return ProbeResult(str(model)[:120], ProbeStatus.ERROR, detail="invalid_name")
        rows = self._model_rows(self._show_timeout)
        for row in rows:
            row_id = str(row.get("id") or row.get("name") or row.get("model") or "")
            if row_id == model:
                raw_sonder = row.get("sonder")
                sonder_meta: dict[str, Any] = raw_sonder if isinstance(raw_sonder, dict) else {}
                ctx = sonder_meta.get("context_length")
                ctx_int = ctx if type(ctx) is int and ctx > 0 else None
                raw_caps = sonder_meta.get("capabilities")
                caps = tuple(str(c) for c in raw_caps) if isinstance(raw_caps, list) else ()
                return ProbeResult(
                    model, ProbeStatus.LIVE, None,
                    None, ctx_int, caps,
                    str(sonder_meta.get("family")) if sonder_meta.get("family") else None,
                    "ok",
                )
        return ProbeResult(model, ProbeStatus.NOT_FOUND, detail="not_found")

    def pull_stub(self, model: str) -> None:
        """Local Ollama ``:cloud`` stub pulls are retired; configure models in SI ``ollama_cloud``."""
        raise OllamaPullFailed("cloud_stub_pull_retired_configure_in_si_ollama_cloud")


__all__ = [
    "MAX_BODY",
    "OllamaCloudMetadata",
    "OllamaPullFailed",
    "OllamaTransportError",
]
