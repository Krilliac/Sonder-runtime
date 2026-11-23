"""CLI entrypoint for ``python -m sonder_runtime.interfaces.client_agent``."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .executor import CLIENT_AGENT_MODES
from .server import (
    DEFAULT_CLIENT_AGENT_HOST,
    DEFAULT_CLIENT_AGENT_PORT,
    create_client_agent_server,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m sonder_runtime.interfaces.client_agent",
        description="Local client companion for Sonder execution_target='client' tools.",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_CLIENT_AGENT_HOST,
        help="Loopback bind host (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_CLIENT_AGENT_PORT,
        help="Port to listen on (default: 11439).",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace root directory for client tool operations (default: current dir).",
    )
    parser.add_argument(
        "--mode",
        choices=CLIENT_AGENT_MODES,
        default="manual",
        help="Client-local permission mode (default: manual).",
    )
    parser.add_argument(
        "--max-mode",
        choices=CLIENT_AGENT_MODES,
        default=None,
        help="Maximum permission mode reachable via /v1/client-agent/mode.",
    )
    parser.add_argument(
        "--confirm-bypass",
        action="store_true",
        help="Required to allow bypass mode at startup or via /v1/client-agent/mode.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("SONDER_CLIENT_AGENT_TOKEN", ""),
        help="Shared bearer token (auto-generated if omitted).",
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="Additional allowed browser origin (e.g. https://your-host.trycloudflare.com).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if (args.mode == "bypass" or args.max_mode == "bypass") and not args.confirm_bypass:
        print(
            "Error: bypass mode requires --confirm-bypass.",
            file=sys.stderr,
        )
        return 2
    env_origins = [
        item.strip()
        for item in os.environ.get("SONDER_CLIENT_AGENT_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    ]
    allowed_origins = [*env_origins, *args.allow_origin]
    server = create_client_agent_server(
        host=args.host,
        port=args.port,
        workspace_root=Path(args.workspace),
        mode=args.mode,
        max_mode=args.max_mode,
        allow_bypass=bool(args.confirm_bypass),
        token=args.token or None,
        allowed_origins=allowed_origins,
    )
    bound_host, bound_port = server.server_address[:2]
    host_text = bound_host.decode("ascii", errors="replace") if isinstance(bound_host, bytes) else str(bound_host)
    url_host = f"[{host_text}]" if ":" in host_text and not host_text.startswith("[") else host_text
    print(f"Sonder Client Agent listening on http://{url_host}:{bound_port}")
    print(f"Workspace: {server.policy.workspace_root}")
    print(f"Mode:      {server.policy.mode} (max: {server.policy.max_mode}, can_bypass: {server.policy.can_bypass})")
    print(f"Token:     {server.token}")
    if server.allowed_origins:
        print(f"Origins:   {', '.join(sorted(server.allowed_origins))}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
