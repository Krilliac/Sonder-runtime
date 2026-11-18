"""Write the sonder_tool SSE fixtures from the server's real emitter.

Run from the repo root:  PYTHONPATH=. python app/test/fixtures/server/generate_tool_stream.py <out-dir>

It drives ``ChatToolTurn`` (the code that emits ``sonder_tool`` payloads) with
a stand-in model, a stand-in tool backend and a scripted approval outcome,
then frames every emitted payload with the server's own ``_chunk``. Nothing
here is hand-written JSON, so the fixtures cannot drift from the emitter
without the output changing when this script is re-run.
"""
import os
import sys
import tempfile
from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch

import permission_modes as pm
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.chat_run_backend import ChatRunBackend
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.application.chat.tool_loop import ChatToolTurn, build_tool_turn_manifest
from sonder_runtime.domain.chat_tools import chat_tools_payload
from sonder_runtime.interfaces.http.serve import _chunk

out = sys.argv[1]
os.makedirs(out, exist_ok=True)
tmp = Path(tempfile.mkdtemp())
mp = MonkeyPatch()
ledger = ApprovalLedger(tmp / "approvals.db")
mp.setattr(pm, "_approval_ledger", lambda: ledger)
mp.setattr(pm, "_rule_lookup", lambda _n: None)
mp.setenv("SONDER_PERMISSION_STATE", str(tmp / "permission_mode.json"))
mp.setattr(ts, "API_KEY", "")
mp.setattr(ts, "AUTH_MODE", "local-open")
mp.setattr(ts, "REQUIRE_ACCOUNT", False)
pm._reset_for_tests()
pm.set_mode(pm.MANUAL)

IID = "a1b2c3d4e5f6"
CODE = "print('hi')\nprint(6 * 7)"


def run_turn(outcome, code):
    events = []
    manifest = build_tool_turn_manifest(admitted_tools=["run_code"], user_run_command=True, mode="manual")
    backend = ChatRunBackend(
        dispatch_agent_tool=lambda name, args, **_kw: "hi\n42",
        catalog_provider=dict,
        permission_policy=pm,
        blocked_tools=frozenset(),
    )

    def wait_hook(_tool, call_id, _decision, _preview):
        if outcome == "approved":
            pending = ledger.resolve_call(call_id)
            ledger.issue(pending.tool, pending.digest, approver="fixture")
        return outcome

    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=chat_tools_payload(allowed_names=["run_code"]),
        backend=backend,
        permission_policy=pm,
        on_tool_event=events.append,
        wait_for_approval=wait_hook,
    )
    steps = iter([
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "run_code", "arguments": {"code": code}}}]}},
        {"message": {"role": "assistant", "content": "done"}},
    ])
    turn.run(lambda _m, _t: next(steps), system="sys", prompt="run", history=None)
    return events


def frames(events):
    return "".join(_chunk(IID, "sonder", {}, sonder_tool=e) for e in events)


for name, outcome, code in (
    ("tool_stream_approved.sse", "approved", CODE),
    ("tool_stream_denied.sse", "denied", CODE),
    ("tool_stream_expired.sse", "expired", CODE),
    ("tool_stream_bidi_long.sse", "denied", "x = '\u202e'\n" + "y = 1\n" * 1200),
):
    with open(os.path.join(out, name), "w", newline="") as f:
        f.write(frames(run_turn(outcome, code)))
mp.undo()
