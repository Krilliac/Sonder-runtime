"""Minimal authenticated supervisor for starting Trilobite from mobile apps.

This process is intentionally independent from server.py and exposes only
status/start/stop/restart. It is not a shell and accepts no executable paths or
arbitrary arguments from clients.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
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

    def _run(self, action, context_size="8192"):
        if action not in {"start", "stop", "restart", "status"}:
            raise ValueError("unsupported launcher action")
        command = [*self.command_base, action]
        if action in {"start", "restart"}:
            command.extend([
                "--host", self.server_host,
                "--port", str(self.server_port),
                "--context-size", str(context_size or "8192")[:32],
            ])
        env = os.environ.copy()
        env.setdefault("TRILOBITE_HOST", self.server_host)
        env.setdefault("TRILOBITE_PORT", str(self.server_port))
        result = subprocess.run(
            command, cwd=self.root, env=env, text=True, capture_output=True,
            timeout=90, check=False,
        )
        output = "\n".join(
            value.strip() for value in (result.stdout, result.stderr)
            if value and value.strip()
        )[:20_000]
        return result.returncode, output, command

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
            if action == "start" and _reachable("127.0.0.1", self.server_port):
                return {**self.status(), "message": "Trilobite server is already running."}
            code, output, command = self._run(action, context_size)
            self.last_action = action
            self.last_action_ts = int(time.time())
            self.last_error = "" if code == 0 else output or "launcher command failed"
            if action in {"start", "restart"} and code == 0:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if _reachable("127.0.0.1", self.server_port):
                        break
                    time.sleep(0.25)
            payload = self.status()
            payload.update({
                "ok": code == 0 and (action == "stop" or payload["server_running"]),
                "message": output or "%s requested" % action,
                "command": [Path(command[0]).name, Path(command[1]).name, *command[2:]],
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
        context_size = str(body.get("context_size") or "8192")
        if not context_size.lower().rstrip("km").replace(".", "", 1).isdigit():
            self._send({"ok": False, "error": "invalid context_size"}, 400)
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
