"""Launch the real ``sonder repl`` with a fake build service behind the typed gateway.

Same pinning as ``_repl_launch`` (banner provenance), plus one seam: the
REPL's ``_typed_tools`` returns a stand-in gateway whose registry is the real
typed tool registry and whose ``execute`` answers the build tools with fixed
payloads. Everything in front of it -- the slash chain, the usage check, the
console permission gate and its approval prompt, ``_build_execute_tool`` and
the build facade -- is the production code, unpatched. The stand-in refuses
any request that does not say ``source="repl"`` and ``gate="surface"``, so a
screen that shows a result also proves the console forwarded its decision.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from tests.repl import _repl_launch  # noqa: F401 - pins the banner facts
from sonder_runtime.interfaces.repl import repl as _repl

JOB = "build-fix-" + "0123456789abcdef"

_PAYLOADS = {
    "build_model": {
        "object": "build_model", "status": "ok", "system": "cmake", "target": "game",
        "config": "Debug", "targets": ["game", "engine_core", "tests"],
    },
    "build_fix": {
        "object": "build_fix_status", "job_id": JOB, "status": "running", "target": "game",
        "config": "Debug", "world": "local", "network": "off",
        "display_command": ["cmake", "--build", "[BUILD]", "--target", "game"],
        "next": "/fix-build status %s" % JOB,
    },
}


class _FakeBuildGateway:
    def __init__(self):
        from sonder_runtime.bootstrap.typed_tools import typed_tool_registry

        self.graph = SimpleNamespace(registry=typed_tool_registry())

    def execute(self, request):
        scope = request.scope
        if scope.source != "repl" or scope.gate != "surface":
            payload = {"error_code": "NOT_A_CONSOLE_DECISION", "message": "fake gateway"}
            return SimpleNamespace(success=False, output=json.dumps(payload),
                                   error_code=payload["error_code"], error="")
        payload = _PAYLOADS.get(request.tool_name)
        if payload is None:
            return SimpleNamespace(success=False, output="{}", error_code="UNEXPECTED_TOOL",
                                   error=request.tool_name)
        return SimpleNamespace(success=True, output=json.dumps(payload), error_code="", error="")


_GATEWAY = _FakeBuildGateway()
_repl._typed_tools = lambda: _GATEWAY


if __name__ == "__main__":
    from sonder_runtime.__main__ import main

    sys.exit(main(["repl", *sys.argv[1:]]))
