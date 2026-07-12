"""Minimal authenticated supervisor for starting Trilobite from mobile apps.

This process is intentionally independent from server.py and exposes only
status/start/stop/restart. It is not a shell and accepts no executable paths or
arbitrary arguments from clients.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hmac
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11436
SERVER_PORT = 11435
MAX_BODY = 16_384
MAX_CONTEXT_TOKENS = 1_000_000
ACTION_TIMEOUT_SECONDS = 40.0
POLL_INTERVAL_SECONDS = 0.25
_CONTEXT_SIZE = re.compile(r"^(\d{1,7})(?:\.(\d{1,3}))?([km]?)$")


def _loopback(host):
    value = str(host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return socket.gethostbyname(value).startswith("127.") or value == "::1"
    except OSError:
        return False


def _reachable(host="127.0.0.1", port=SERVER_PORT, timeout=0.4):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def normalize_context_size(value):
    """Validate the bounded context syntax accepted by the main server."""
    text = str(value or "8192").strip().lower()
    match = _CONTEXT_SIZE.fullmatch(text)
    if not match:
        raise ValueError("invalid context_size")
    try:
        number = Decimal(match.group(1) + ("." + match.group(2) if match.group(2) else ""))
    except InvalidOperation as exc:  # Defensive: the regular expression is stricter.
        raise ValueError("invalid context_size") from exc
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(3)]
    tokens = number * multiplier
    if tokens < 1 or tokens > MAX_CONTEXT_TOKENS:
        raise ValueError(
            "context_size must resolve to between 1 and %s tokens"
            % MAX_CONTEXT_TOKENS
        )
    if tokens != tokens.to_integral_value():
        raise ValueError("context_size must resolve to a whole number of tokens")
    return text


def _output_text(*values):
    chunks = []
    for value in values:
        if not value:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        value = str(value).strip()
        if value:
            chunks.append(value)
    return "\n".join(chunks)[:20_000]


class LauncherController:
    def __init__(self, root=ROOT, python=sys.executable, server_host="0.0.0.0", server_port=SERVER_PORT):
        self.root = Path(root).resolve()
        self.python = str(python)
        self.server_host = str(server_host)
        self.server_port = int(server_port)
        self._lock = threading.RLock()
        self.last_action = ""
        self.last_error = ""
        self.last_action_ts = 0

    @property
    def command_base(self):
        return [self.python, str(self.root / "trilobite_headless.py")]

    def _run(self, action, context_size="8192", timeout=ACTION_TIMEOUT_SECONDS):
        if action not in {"start", "stop", "restart", "status"}:
            raise ValueError("unsupported launcher action")
        command = [*self.command_base, action]
        command.extend([
            "--host", self.server_host,
            "--port", str(self.server_port),
        ])
        if action in {"start", "restart"}:
            command.extend(["--context-size", normalize_context_size(context_size)])
        env = os.environ.copy()
        # Controller configuration is authoritative; stale parent variables must
        # not redirect the child to a different interface or port.
        env["TRILOBITE_HOST"] = self.server_host
        env["TRILOBITE_PORT"] = str(self.server_port)
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                env=env,
                text=True,
                capture_output=True,
                timeout=max(0.1, float(timeout)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = _output_text(exc.stdout, exc.stderr)
            detail = "launcher command timed out after %.1f seconds" % max(
                0.1, float(timeout)
            )
            return 124, _output_text(output, detail), command
        except OSError as exc:
            return 126, "launcher command could not start: %s" % exc, command
        return result.returncode, _output_text(result.stdout, result.stderr), command

    def _wait_for_state(self, running, deadline):
        while True:
            if _reachable("127.0.0.1", self.server_port) == bool(running):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(POLL_INTERVAL_SECONDS, remaining))

    def status(self):
        running = _reachable("127.0.0.1", self.server_port)
        return {
            "ok": True,
            "launcher": "ready",
            "server_running": running,
            "server_host": self.server_host,
            "server_port": self.server_port,
            "last_action": self.last_action,
            "last_action_ts": self.last_action_ts,
            "last_error": self.last_error,
        }

    def action(self, action, context_size="8192"):
        with self._lock:
            if action not in {"start", "stop", "restart"}:
                raise ValueError("unsupported launcher action")
            context_size = normalize_context_size(context_size)
            if action == "start" and _reachable("127.0.0.1", self.server_port):
                return {**self.status(), "message": "Trilobite server is already running."}

            self.last_action = action
            self.last_action_ts = int(time.time())
            deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
            steps = ("stop", "start") if action == "restart" else (action,)
            outputs = []
            commands = []
            failure = ""
            for step in steps:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "launcher action timed out before %s could run" % step
                    break
                code, output, command = self._run(step, context_size, remaining)
                commands.append(
                    [Path(command[0]).name, Path(command[1]).name, *command[2:]]
                )
                if output:
                    outputs.append("%s: %s" % (step, output))
                if code != 0:
                    failure = output or "%s command failed with exit code %s" % (step, code)
                    break
                expected_running = step == "start"
                if not self._wait_for_state(expected_running, deadline):
                    failure = (
                        "server did not become reachable before the deadline"
                        if expected_running
                        else "server remained reachable after the stop request"
                    )
                    break

            payload = self.status()
            expected_running = action != "stop"
            if not failure and payload["server_running"] is not expected_running:
                failure = (
                    "server is not reachable after the %s request" % action
                    if expected_running
                    else "server is still reachable after the stop request"
                )
            self.last_error = failure
            payload["last_error"] = failure
            failure_is_reported = any(failure and failure in output for output in outputs)
            message_parts = outputs + (
                [failure] if failure and not failure_is_reported else []
            )
            payload.update({
                "ok": not failure,
                "message": "\n".join(message_parts) or "%s completed" % action,
                "command": commands[-1] if commands else [],
                "commands": commands,
            })
            return payload


class LauncherServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, controller, token):
        super().__init__(address, handler)
        self.controller = controller
        self.token = token


class LauncherHandler(BaseHTTPRequestHandler):
    server_version = "TrilobiteLauncher/1"

    def log_message(self, fmt, *args):
        if os.environ.get("TRILOBITE_LAUNCHER_QUIET") != "1":
            super().log_message(fmt, *args)

    def _authorized(self):
        expected = self.server.token
        if not expected:
            return _loopback(self.client_address[0])
        supplied = self.headers.get("Authorization", "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        else:
            supplied = self.headers.get("X-Trilobite-Launcher-Token", "").strip()
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def _send(self, payload, status=200):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if self._authorized():
            return True
        self._send({"ok": False, "error": "launcher authentication required"}, 401)
        return False

    def do_GET(self):
        if self.path.rstrip("/") not in {"", "/v1/launcher/status"}:
            self._send({"ok": False, "error": "not found"}, 404)
            return
        if not self._auth():
            return
        self._send(self.server.controller.status())

    def do_POST(self):
        path = self.path.rstrip("/")
        action = path.rsplit("/", 1)[-1]
        if path not in {
            "/v1/launcher/start", "/v1/launcher/stop", "/v1/launcher/restart",
        }:
            self._send({"ok": False, "error": "not found"}, 404)
            return
        if not self._auth():
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._send({"ok": False, "error": "invalid content length"}, 400)
            return
        if length < 0 or length > MAX_BODY:
            self._send({"ok": False, "error": "request too large"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._send({"ok": False, "error": "invalid JSON"}, 400)
            return
        if not isinstance(body, dict):
            self._send({"ok": False, "error": "JSON body must be an object"}, 400)
            return
        try:
            context_size = normalize_context_size(body.get("context_size") or "8192")
        except ValueError as exc:
            self._send({"ok": False, "error": str(exc)}, 400)
            return
        payload = self.server.controller.action(action, context_size)
        self._send(payload, 200 if payload.get("ok") else 503)


def generate_token():
    return secrets.token_urlsafe(32)


def validate_configuration(host, token):
    if not _loopback(host) and len(token) < 24:
        raise ValueError("LAN launcher binding requires TRILOBITE_LAUNCHER_TOKEN with at least 24 characters")


def serve(host, port, token, controller=None, cert="", key=""):
    validate_configuration(host, token)
    server = LauncherServer(
        (host, int(port)), LauncherHandler,
        controller=controller or LauncherController(), token=token,
    )
    if cert or key:
        if not cert or not key:
            raise ValueError("both TLS certificate and key are required")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    print("Trilobite launcher listening on %s://%s:%s" % ("https" if cert else "http", host, port))
    server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("TRILOBITE_LAUNCHER_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRILOBITE_LAUNCHER_PORT", DEFAULT_PORT)))
    parser.add_argument("--token", default=os.environ.get("TRILOBITE_LAUNCHER_TOKEN", ""))
    parser.add_argument("--server-host", default=os.environ.get("TRILOBITE_HOST", "0.0.0.0"))
    parser.add_argument("--server-port", type=int, default=int(os.environ.get("TRILOBITE_PORT", SERVER_PORT)))
    parser.add_argument("--cert", default=os.environ.get("TRILOBITE_LAUNCHER_CERT", ""))
    parser.add_argument("--key", default=os.environ.get("TRILOBITE_LAUNCHER_KEY", ""))
    parser.add_argument("--generate-token", action="store_true")
    args = parser.parse_args(argv)
    if args.generate_token:
        print(generate_token())
        return 0
    controller = LauncherController(server_host=args.server_host, server_port=args.server_port)
    try:
        serve(args.host, args.port, args.token, controller, args.cert, args.key)
    except (OSError, ValueError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
