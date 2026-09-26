"""sonder_client — thin remote client (run from a checkout) for a hosted Sonder Runtime.

Run this file from a checkout of the repository: it needs only the Python
standard library plus the checkout's ``sonder_runtime`` client adapters (no
server/memory_store/mcp/ollama imports and no pip installs). It is not a
single-file download. Remote hosts keep the runtime on loopback behind the
documented TLS reverse proxy; see CLIENT.md.

Config (env or argv):
    SONDER_SERVER   e.g. https://sonder.example.com   (required)
    SONDER_API_KEY  optional bearer key, if the server has auth enabled; sent
                    only over https:// (or http:// to a loopback host) and
                    never across a redirect
    SONDER_LOCAL_FALLBACK  default http://127.0.0.1:11435
    SONDER_FALLBACK_LOCAL=0 disables local fallback
    --server URL       argv override for SONDER_SERVER
    --key K            argv override for SONDER_API_KEY

Run:
    python sonder_client.py
    python sonder_client.py --server https://sonder.example.com --key s3cret
"""
import sys
import urllib.error

from sonder_runtime.adapters.client_endpoint import (
    local_fallback_server as _local_fallback_server,
    same_server as _same_server,
)
from sonder_runtime.adapters.client_fallback import (
    send_prompt_with_fallback as _send_prompt_with_fallback,
)
from sonder_runtime.adapters.client_request import (
    InsecureKeyTransportError as _InsecureKeyTransportError,
    build_chat_request as _build_chat_request,
    require_secure_key_transport as _require_secure_key_transport,
)
from sonder_runtime.adapters.client_transport import (
    send_chat_prompt as _send_chat_prompt,
)
from sonder_runtime.adapters.client_config import (
    parse_argv as _parse_argv,
    resolve_config as _resolve_config,
)
from sonder_runtime.platform.client_fallback import enabled as local_fallback_enabled

LOCAL_FALLBACK_SERVER = _local_fallback_server()

USAGE = """usage: sonder_client.py [--server URL] [--key API_KEY]

Set SONDER_SERVER (and optionally SONDER_API_KEY) in the environment,
or pass --server/--key on the command line.

Example:
    set SONDER_SERVER=https://sonder.example.com
    set SONDER_API_KEY=s3cret
    python sonder_client.py
"""


def build_request(server, api_key, prompt):
    """Compatibility delegate for the packaged standalone-client adapter."""
    return _build_chat_request(server, api_key, prompt)


def send_prompt(server, api_key, prompt):
    """Send a prompt to the hosted Sonder Runtime; returns the assistant's reply text,
    or raises on a network/HTTP error (caller handles presentation)."""
    return _send_chat_prompt(
        server, api_key, prompt, request_builder=build_request
    )


def send_prompt_with_fallback(server, api_key, prompt, fallback_server=None):
    """Compatibility delegate for packaged fallback orchestration."""
    return _send_prompt_with_fallback(
        server, api_key, prompt, fallback_server or LOCAL_FALLBACK_SERVER,
        sender=send_prompt,
        fallback_policy=local_fallback_enabled,
    )


def resolve_config(argv):
    """Compatibility delegate for the packaged client configuration adapter."""
    return _resolve_config(argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    server, api_key = resolve_config(argv)

    if not server:
        print(USAGE)
        return 1

    if api_key:
        try:
            _require_secure_key_transport(server)
        except _InsecureKeyTransportError as e:
            print("error: %s" % e)
            return 2

    print("Sonder Runtime (remote) — connected to %s" % server)

    if local_fallback_enabled() and not _same_server(server, LOCAL_FALLBACK_SERVER):
        print("local fallback: %s (set SONDER_FALLBACK_LOCAL=0 to disable)" % LOCAL_FALLBACK_SERVER)

    while True:
        try:
            line = input("sonder> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        stripped = line.strip()
        if stripped in ("/exit", "/quit"):
            return 0
        if not stripped:
            continue

        try:
            reply, _used_server, warning = send_prompt_with_fallback(server, api_key, line)
            if warning:
                print(warning)
            print(reply)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = str(e)
            print("HTTP %s: %s" % (e.code, err_body))
        except urllib.error.URLError as e:
            print("connection error: %s" % e)
        except Exception as e:
            print("error: %s" % e)


if __name__ == "__main__":
    sys.exit(main())
