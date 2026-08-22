"""sonder_client — standalone thin remote client for a hosted Sonder Runtime.

Drop this single file on any PC with Python (stdlib only — no server/memory_store/
mcp/ollama imports) to talk to a Sonder Runtime instance hosted elsewhere (e.g. a VPS
running sonder_serve.py with SONDER_HOST=0.0.0.0).

Config (env or argv):
    SONDER_SERVER   e.g. http://your-vps:11435   (required)
    SONDER_API_KEY  optional bearer key, if the server has auth enabled
    SONDER_LOCAL_FALLBACK  default http://127.0.0.1:11435
    SONDER_FALLBACK_LOCAL=0 disables local fallback
    --server URL       argv override for SONDER_SERVER
    --key K            argv override for SONDER_API_KEY

Run:
    python sonder_client.py
    python sonder_client.py --server http://your-vps:11435 --key s3cret
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
    build_chat_request as _build_chat_request,
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
    set SONDER_SERVER=http://your-vps:11435
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
