"""S6: the host launcher applies the same DNS-rebinding Host policy.

A token-less loopback launcher authorizes any loopback peer, and a rebinding
page is same-origin, so without a Host check it could POST
``/v1/launcher/stop`` from a local browser.
"""
from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager

import pytest

import sonder_launcher
from tests.test_launcher import FakeController

pytestmark = pytest.mark.unit

TOKEN = "t" * 32


@contextmanager
def _launcher(tmp_path, *, token="", allowed_hosts=()):
    server = sonder_launcher.LauncherServer(
        ("127.0.0.1", 0), sonder_launcher.LauncherHandler,
        controller=FakeController(), token=token,
        command_journal=sonder_launcher.command_recovery.CommandJournal(
            tmp_path / "launcher-commands.jsonl"),
        allowed_hosts=allowed_hosts, local_names={"mypc", "mypc.local"},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _call(port, host, method="GET", path="/v1/launcher/status", token=""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        if token:
            conn.putheader("Authorization", "Bearer " + token)
        body = b"{}" if method == "POST" else b""
        if method == "POST":
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body or None)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_tokenless_launcher_refuses_a_rebinding_name(tmp_path):
    with _launcher(tmp_path) as port:
        for method, path in (("GET", "/v1/launcher/status"), ("POST", "/v1/launcher/stop")):
            status, body = _call(port, "evil.example:%d" % port, method, path)
            assert status == 421
            payload = json.loads(body)
            assert payload["code"] == "HOST_NOT_ALLOWED"
            assert "SONDER_LAUNCHER_ALLOWED_HOSTS" in payload["remedy"]


@pytest.mark.parametrize("host", ["127.0.0.1:11436", "localhost", "10.0.2.2:11436",
                                  "192.168.1.20:8443", "mypc.local", None])
def test_tokenless_launcher_serves_addresses_and_machine_names(tmp_path, host):
    with _launcher(tmp_path) as port:
        assert _call(port, host)[0] == 200


def test_listed_name_is_served_without_a_token(tmp_path):
    with _launcher(tmp_path, allowed_hosts=[("launcher.example", None)]) as port:
        assert _call(port, "launcher.example")[0] == 200
        assert _call(port, "other.example")[0] == 421


def test_token_launcher_accepts_any_name_and_still_needs_the_token(tmp_path):
    with _launcher(tmp_path, token=TOKEN) as port:
        assert _call(port, "launcher.tail1234.ts.net")[0] == 401
        assert _call(port, "launcher.tail1234.ts.net", token=TOKEN)[0] == 200


def test_allowed_hosts_env_is_parsed_and_junk_dropped():
    parsed = sonder_launcher.parse_launcher_allowed_hosts("a.example, bad host,b.example:443,")
    assert parsed == (("a.example", None), ("b.example", 443))
