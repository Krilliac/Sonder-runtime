"""Shared fixtures for the cloud model refresh tests: a loopback fake Ollama and a tmp world.

Nothing here touches the real state home or the network.  The fake serves
only ``/api/tags``, ``/api/show`` and ``/api/pull`` and records every
request, so a test can prove no chat/generate/embed call was ever made.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from sonder_runtime.adapters import model_lists

# The provider's retirement answer, byte for byte, as captured from a real
# ``POST /api/show {"model": "deepseek-v4-flash:cloud"}`` on 2026-11-19
# (HTTP 410).  The live API appends a ``(ref: <uuid>)`` request reference.
MEASURED_RETIREMENT_STATUS = 410
MEASURED_RETIREMENT = (
    "deepseek-v4-flash:0731 was retired on September 25, 2026. Try glm-5.3-flash:cloud. "
    "(ref: 59bcf5d7-6731-4254-858b-f8c8705b9dbe)"
)
MEASURED_RETIREMENT_REF = "59bcf5d7-6731-4254-858b-f8c8705b9dbe"


def retired_message(provider: str, suggestion: str | None, date: str = "September 25, 2026",
                    ref: str | None = MEASURED_RETIREMENT_REF) -> str:
    """A retirement notice in the live shape (with the ``(ref: ...)`` tail unless ``ref=None``)."""
    text = f"{provider} was retired on {date}."
    text += f" Try {suggestion}." if suggestion else ""
    return text + (f" (ref: {ref})" if ref else "")


LIVE_SHOW = {
    "details": {"family": "fakefam", "parameter_size": "1.2T"},
    "model_info": {"fakefam.context_length": 262144, "general.architecture": "fakefam"},
    "capabilities": ["completion", "tools", "thinking"],
}


class FakeOllama:
    """Threaded loopback Ollama with per-model ``/api/show`` answers."""

    def __init__(self, *, tags: tuple[str, ...] = (), shows: dict[str, tuple[int, Any]] | None = None,
                 pull_status: int = 200, pull_payload: Any = None,
                 drop_show_after: int | None = None, tags_status: int = 200) -> None:
        self.tags = list(tags)
        self.shows = dict(shows or {})
        self.pull_status = pull_status
        self.pull_payload = {"status": "success"} if pull_payload is None else pull_payload
        self.drop_show_after = drop_show_after
        self.tags_status = tags_status
        self.requests: list[tuple[str, str, Any]] = []
        self._lock = threading.Lock()
        self._shows_seen = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                with owner._lock:
                    owner.requests.append(("GET", self.path, None))
                if self.path.startswith("/api/tags"):
                    rows = [{"name": n, "model": n, "size": 1} for n in owner.tags]
                    if owner.tags_status != 200:
                        return self._send(owner.tags_status, {"error": "tags unavailable"})
                    return self._send(200, {"models": rows})
                return self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
                with owner._lock:
                    owner.requests.append(("POST", self.path, payload))
                if self.path.startswith("/api/show"):
                    with owner._lock:
                        owner._shows_seen += 1
                        seen = owner._shows_seen
                    if owner.drop_show_after is not None and seen > owner.drop_show_after:
                        self.close_connection = True
                        return
                    status, body = owner.shows.get(payload.get("model", ""), (200, LIVE_SHOW))
                    return self._send(status, body)
                if self.path.startswith("/api/pull"):
                    if owner.pull_status == 200 and "error" not in owner.pull_payload:
                        with owner._lock:
                            owner.tags.append(payload["model"])
                    return self._send(owner.pull_status, owner.pull_payload)
                return self._send(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = "http://127.0.0.1:%d" % self._server.server_address[1]

    def paths(self, method: str | None = None) -> list[str]:
        return [path for verb, path, _ in self.requests if method in (None, verb)]

    def shown(self) -> list[str]:
        return [body["model"] for verb, path, body in self.requests if path == "/api/show"]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@dataclass
class World:
    """A hermetic refresh world: tmp home, overlay path, fake Ollama."""

    home: Path
    overlay: Path
    fake: FakeOllama
    env: dict[str, str] = field(default_factory=dict)

    @property
    def preferences(self) -> Path:
        return self.home / "cloud_model_preferences.json"

    @property
    def state(self) -> Path:
        return self.home / "cloud_model_catalog.json"

    @property
    def change_log(self) -> Path:
        return self.home / "cloud_model_changes.jsonl"

    def write_preferences(self, document: dict[str, Any]) -> None:
        self.preferences.write_text(json.dumps(document), encoding="utf-8")

    def write_overlay(self, document: dict[str, Any]) -> None:
        self.overlay.write_text(json.dumps(document), encoding="utf-8")
        model_lists.clear_cache()

    def run(self, *, apply: bool, **kwargs: Any) -> dict[str, Any]:
        from sonder_runtime.adapters import cloud_model_refresh

        kwargs.setdefault("use_catalog", False)
        return cloud_model_refresh.run_refresh(
            origin=self.fake.url, apply=apply, home=self.home, env=dict(self.env), **kwargs
        )

    def snapshot(self) -> dict[str, str]:
        """sha256 of every non-lock file under the home (the run lock is an empty sidecar)."""
        return snapshot(self.home)


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix != ".lock"
    }


def standard_shows() -> dict[str, tuple[int, Any]]:
    """Retired default code (no successor), retired default general (provider suggests glm-5.3)."""
    return {
        "kimi-k2.7-code:cloud": (410, {"error": retired_message("kimi-k2.7-code:0801", None)}),
        "glm-5.2:cloud": (404, {"error": retired_message("glm-5.2:0801", "glm-5.3:cloud")}),
    }


def make_world(tmp_path: Path, monkeypatch: Any, **fake_kwargs: Any) -> World:
    home = tmp_path / "home"
    home.mkdir()
    overlay = home / "model_lists.json"
    monkeypatch.setenv("SONDER_MODEL_LISTS", str(overlay))
    model_lists.clear_cache()
    fake = FakeOllama(**fake_kwargs)
    return World(home=home, overlay=overlay, fake=fake)
