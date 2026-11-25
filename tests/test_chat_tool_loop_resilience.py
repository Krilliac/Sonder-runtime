"""Resilience tests for ChatToolTurn, chat_tool_stream, argument bounds, and truncation."""
from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from sonder_runtime.adapters.chat_run_backend import ToolExecutionResult, run_chat_tool_turn
from sonder_runtime.application.chat.tool_loop import ChatToolTurn, build_tool_turn_manifest
from sonder_runtime.domain.chat_tools import (
    ChatToolBudget,
    preview_tool_arguments,
    validate_tool_arguments,
)
from sonder_runtime.domain.resilience import (
    GLOBAL_TELEMETRY,
    compact_messages_for_retry,
    repair_tool_arguments,
)
from sonder_runtime.interfaces.http import chat_tool_stream

pytestmark = pytest.mark.unit


class _AllowPolicy:
    def __init__(self, *, allowed: bool = True, source: str = "allow", call_id: str = "call-pol-1") -> None:
        self.allowed = allowed
        self.source = source
        self.call_id = call_id
        self.decide_calls: list[tuple[str, dict[str, Any]]] = []

    def decide(self, tool_name: str, **kwargs: Any) -> Any:
        self.decide_calls.append((tool_name, dict(kwargs.get("arguments") or {})))
        return SimpleNamespace(
            allowed=self.allowed,
            call_id=self.call_id,
            risk="execute" if not self.allowed else "safe",
            mode="manual" if not self.allowed else "auto",
            source=self.source,
            reason="operator approval required" if not self.allowed else "",
        )


class _StubBackend:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def backend_for(self, _name: str) -> str:
        return "workspace"

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        return self._handler(name, args)


def _ok_result(output: str = "ok", **extra: Any) -> ToolExecutionResult:
    return ToolExecutionResult(
        ok=True,
        output=output,
        backend="workspace",
        duration_ms=2,
        exit_code=0,
        stdout_preview=extra.get("stdout_preview", output[:80]),
        stderr_preview=extra.get("stderr_preview", ""),
        reason=extra.get("reason", ""),
    )


# --- 1. Multi-round tokens_in / tokens_out accumulation -----------------------


def test_multi_round_usage_accumulates_normalized_openai_and_ollama_shapes():
    backend = _StubBackend(lambda _n, _a: _ok_result("done"))
    manifest = build_tool_turn_manifest(admitted_tools=("file_read",), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend,
        permission_policy=_AllowPolicy(),
        budget=ChatToolBudget(max_model_calls=5),
    )

    steps = iter([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "file_read", "arguments": '{"path": "a.py"}'}}],
            },
            "usage": {"tokens_in": 120, "tokens_out": 30},
        },
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c2", "function": {"name": "file_read", "arguments": '{"path": "b.py"}'}}],
            },
            "usage": {"prompt_tokens": 250, "completion_tokens": 45},
        },
        {
            "message": {"role": "assistant", "content": "All files inspected."},
            "prompt_eval_count": 300,
            "eval_count": 60,
        },
    ])

    outcome = turn.run(lambda _m, _t: next(steps), system="sys", prompt="inspect")
    assert outcome.content == "All files inspected."
    assert outcome.model_calls == 3
    assert outcome.usage["tokens_in"] == 670
    assert outcome.usage["tokens_out"] == 135
    assert outcome.usage["total_tokens"] == 805
    assert outcome.usage["prompt_tokens"] == 670
    assert outcome.usage["completion_tokens"] == 135
    assert outcome.usage["rounds"] == 3
    assert outcome.usage["token_source"] == "chat_tool_loop"


def test_usage_accumulation_rejects_malformed_negative_bool_and_nonfinite_counts():
    backend = _StubBackend(lambda _n, _a: _ok_result("ok"))
    manifest = build_tool_turn_manifest(admitted_tools=("file_read",), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend,
        permission_policy=_AllowPolicy(),
        budget=ChatToolBudget(max_model_calls=5),
    )

    steps = iter([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "file_read", "arguments": '{"path": "a.py"}'}}],
            },
            "usage": {"tokens_in": True, "tokens_out": -50},
        },
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c2", "function": {"name": "file_read", "arguments": '{"path": "b.py"}'}}],
            },
            "usage": {"tokens_in": "not-a-num", "tokens_out": float("nan")},
        },
        {
            "message": {"role": "assistant", "content": "Finished safely."},
            "usage": {"tokens_in": float("inf"), "prompt_tokens": "42", "tokens_out": "17"},
        },
    ])

    outcome = turn.run(lambda _m, _t: next(steps), system="sys", prompt="inspect")
    assert outcome.content == "Finished safely."
    assert outcome.usage["tokens_in"] == 42
    assert outcome.usage["tokens_out"] == 17
    assert outcome.usage["total_tokens"] == 59


def test_run_chat_tool_turn_extracts_openai_ollama_and_malformed_usage():
    backend = _StubBackend(lambda _n, _a: _ok_result("ok"))
    manifest = build_tool_turn_manifest(admitted_tools=("file_read",), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend,
        permission_policy=_AllowPolicy(),
        budget=ChatToolBudget(max_model_calls=4),
    )

    responses = iter([
        (
            {"model": "bonsai-27b", "usage": {"prompt_tokens": 110, "completion_tokens": 25}},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "file_read", "arguments": '{"path": "x.py"}'}}]},
        ),
        (
            {"model": "bonsai-27b", "prompt_eval_count": "bad", "eval_count": True, "usage": {"tokens_in": 90, "tokens_out": 15}},
            {"role": "assistant", "content": "Done."},
        ),
    ])
    outcome = run_chat_tool_turn(
        turn,
        chat_request=lambda *_a, **_kw: next(responses),
        model="bonsai-27b",
        cloud=False,
        options={},
        keep_alive=None,
        system="sys",
        prompt="go",
        history=None,
    )
    assert outcome.content == "Done."
    assert outcome.usage["tokens_in"] == 200
    assert outcome.usage["tokens_out"] == 40


# --- 2. Duplicate & malformed tool_call_id handling + 1-to-1 pairing ----------


def test_duplicate_and_malformed_tool_call_ids_normalized_and_paired_across_rounds():
    GLOBAL_TELEMETRY.reset()
    backend = _StubBackend(lambda _n, a: _ok_result(f"read:{a.get('path', '')}"))
    manifest = build_tool_turn_manifest(admitted_tools=("file_read",), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend,
        permission_policy=_AllowPolicy(),
        budget=ChatToolBudget(max_model_calls=4),
    )

    round1_calls: list[Any] = [
        {"id": "call_dup", "function": {"name": "file_read", "arguments": '{"path": "a.py"}'}},
        {"id": "call_dup", "function": {"name": "file_read", "arguments": '{"path": "b.py"}'}},
        {"id": "bad\nid", "function": {"name": "file_read", "arguments": '{"path": "c.py"}'}},
        {"id": "bidi_\u202e1", "function": {"name": "file_read", "arguments": '{"path": "d.py"}'}},
        {"id": "x" * 200, "function": {"name": "file_read", "arguments": '{"path": "e.py"}'}},
        {"id": 12345, "function": {"name": "file_read", "arguments": '{"path": "f.py"}'}},
        {"id": "call_badfn", "function": "not_a_mapping"},
        "raw_non_mapping_entry",
    ]
    round1_snapshot = json.dumps(round1_calls)

    round2_calls: list[Any] = [
        {"id": "call_dup", "function": {"name": "file_read", "arguments": '{"path": "g.py"}'}},
    ]

    seen_histories: list[list[dict[str, Any]]] = []
    steps = iter([
        {"message": {"role": "assistant", "content": "", "tool_calls": round1_calls}},
        {"message": {"role": "assistant", "content": "", "tool_calls": round2_calls}},
        {"message": {"role": "assistant", "content": "Completed."}},
    ])

    def generate_step(msgs: list[dict[str, Any]], _tools: Any) -> dict[str, Any]:
        seen_histories.append([dict(m) for m in msgs])
        return next(steps)

    outcome = turn.run(generate_step, system="sys", prompt="read all")
    assert outcome.content == "Completed."
    # Input tool_calls list was never mutated in place.
    assert json.dumps(round1_calls) == round1_snapshot

    # Inspect the final history passed to round 3: every assistant tool_call id must be
    # unique across ALL rounds, well-formed, <= 128 chars, and paired 1-to-1 with role="tool".
    final_msgs = seen_histories[2]
    all_assistant_ids: list[str] = []
    idx = 0
    while idx < len(final_msgs):
        msg = final_msgs[idx]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tcs = msg["tool_calls"]
            tc_ids = [tc["id"] for tc in tcs]
            all_assistant_ids.extend(tc_ids)
            tool_replies = final_msgs[idx + 1 : idx + 1 + len(tcs)]
            assert len(tool_replies) == len(tcs)
            assert [m["role"] for m in tool_replies] == ["tool"] * len(tcs)
            assert [m["tool_call_id"] for m in tool_replies] == tc_ids
            idx += 1 + len(tcs)
        else:
            idx += 1

    assert len(all_assistant_ids) == 9
    assert len(set(all_assistant_ids)) == 9
    for cid in all_assistant_ids:
        assert isinstance(cid, str) and 1 <= len(cid) <= 128
        assert not any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cid)
        assert "\u202e" not in cid

    counters = GLOBAL_TELEMETRY.snapshot()["counters"]
    assert counters.get("duplicate_tool_call_id", 0) >= 2
    assert counters.get("malformed_tool_call_id", 0) >= 4


def test_budget_and_per_step_cap_preserve_exact_tool_call_id_pairing():
    backend = _StubBackend(lambda _n, a: _ok_result(f"ok:{a.get('path')}"))
    manifest = build_tool_turn_manifest(admitted_tools=("file_read",), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend,
        permission_policy=_AllowPolicy(),
        budget=ChatToolBudget(max_tool_calls=3, max_tool_calls_per_step=10, max_model_calls=2),
    )

    batch = [
        {"id": f"call_{i}", "function": {"name": "file_read", "arguments": json.dumps({"path": f"f{i}.py"})}}
        for i in range(10)
    ]
    seen: list[list[dict[str, Any]]] = []
    steps = iter([
        {"message": {"role": "assistant", "content": "", "tool_calls": batch}},
        {"message": {"role": "assistant", "content": "Summarized."}},
    ])

    def generate_step(msgs: list[dict[str, Any]], _tools: Any) -> dict[str, Any]:
        seen.append([dict(m) for m in msgs])
        return next(steps)

    outcome = turn.run(generate_step, system="sys", prompt="read")
    assert outcome.content == "Summarized."
    assert len(backend.calls) == 3

    # Assistant message must ONLY keep the 4 handled calls (3 ok + 1 budget_exceeded),
    # matching the 4 role="tool" messages 1-to-1 with zero orphaned tool_call_ids.
    round2_msgs = seen[1]
    asst_msgs = [m for m in round2_msgs if m.get("role") == "assistant"]
    tool_msgs = [m for m in round2_msgs if m.get("role") == "tool"]
    assert len(asst_msgs) == 1
    assert len(asst_msgs[0]["tool_calls"]) == 4
    assert len(tool_msgs) == 4
    assert [tc["id"] for tc in asst_msgs[0]["tool_calls"]] == [m["tool_call_id"] for m in tool_msgs]
    assert [tc["id"] for tc in asst_msgs[0]["tool_calls"]] == ["call_0", "call_1", "call_2", "call_3"]


# --- 3. Argument size, depth, circularity, non-finite, and malformed bounds ---


def test_tool_argument_size_depth_nonfinite_circular_and_malformed_fail_closed():
    GLOBAL_TELEMETRY.reset()
    policy = _AllowPolicy()
    backend = _StubBackend(lambda _n, _a: _ok_result("should-not-run"))
    manifest = build_tool_turn_manifest(admitted_tools=("directory_tree",), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend,
        permission_policy=policy,
        budget=ChatToolBudget(max_argument_chars=256, max_argument_depth=4),
    )

    deep_json = '{"a":' * 8 + '1' + '}' * 8
    deep_dict: dict[str, Any] = {}
    cur = deep_dict
    for _ in range(8):
        nxt: dict[str, Any] = {}
        cur["a"] = nxt
        cur = nxt

    circular: dict[str, Any] = {}
    circular["self"] = circular

    rejected_inputs: list[Any] = [
        json.dumps({"path": "A" * 500}),       # exceeds max_argument_chars (string)
        {"path": "A" * 500},                   # exceeds max_argument_chars (mapping)
        deep_json,                             # exceeds max_argument_depth (string)
        deep_dict,                             # exceeds max_argument_depth (mapping)
        circular,                              # circular mapping
        '{"limit": NaN}',                      # non-finite JSON constant
        '{"limit": Infinity}',                 # non-finite JSON constant
        {"limit": float("nan")},               # non-finite float in mapping
        "[1, 2, 3]",                           # non-object JSON array
        42,                                    # non-object int
        '{"path": "unterminated',              # malformed truncated JSON
    ]

    for bad_args in rejected_inputs:
        obs, _ = turn._run_tool_call(
            {"function": {"name": "directory_tree", "arguments": bad_args}},
            run_count=0,
        )
        assert obs.startswith("ERROR: invalid arguments for tool 'directory_tree':"), (bad_args, obs)

    assert backend.calls == []
    assert policy.decide_calls == []
    assert all(r.status == "error" for r in turn.records)
    assert GLOBAL_TELEMETRY.snapshot()["counters"].get("tool_args_rejected", 0) == len(rejected_inputs)

    # Valid empty and repairable arguments still execute cleanly.
    for valid_args in (None, "", "{}", {}, "{'path': '.',}"):
        parsed, _rep, err = validate_tool_arguments(valid_args, max_chars=256, max_depth=4)
        assert err == "", (valid_args, err)
        assert isinstance(parsed, dict)

    # Direct repair_tool_arguments also enforces size, depth, and non-finite guards without crashing.
    extreme_depth = '{"x":' * 2000 + '1' + '}' * 2000
    assert repair_tool_arguments(extreme_depth, max_chars=50_000, max_depth=16) == ({}, False)
    assert repair_tool_arguments('{"x": NaN}') == ({}, False)


def test_preview_tool_arguments_bounds_nested_collections_and_circular_refs():
    huge_nested = {"items": ["v" * 200 for _ in range(50)]}
    preview = preview_tool_arguments("file_write", huge_nested)
    assert isinstance(preview["arguments"]["items"], str)
    assert len(preview["arguments"]["items"]) <= 520

    circular: dict[str, Any] = {"name": "loop"}
    circular["self"] = circular
    preview_circ = preview_tool_arguments("file_read", circular)
    assert isinstance(preview_circ["arguments"], dict)
    assert "name" in preview_circ["arguments"]


def test_compact_messages_for_retry_never_expands_short_messages():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
        {"role": "tool", "name": "file_read", "content": "short 40 chars message right here ok"},
        {"role": "assistant", "content": "tail"},
    ]
    out, saved = compact_messages_for_retry(msgs, max_tool_chars=10, keep_recent=1)
    assert saved == 0
    assert out[2]["content"] == msgs[2]["content"]


# --- 4. Cancel propagation mid-tool-call & chat_tool_stream concurrency -------


def test_wait_for_approval_checks_abort_first_multi_waiters_and_nonfinite_params():
    class _FakeLedger:
        def __init__(self, *, open_now: bool = False, denied_now: bool = False) -> None:
            self.open_now = open_now
            self.denied_now = denied_now

        def open_for(self, _tool: str, _digest: str) -> bool:
            return self.open_now

        def denied_for(self, _tool: str, _digest: str) -> bool:
            return self.denied_now

    # 1. Already-aborted returns "aborted" BEFORE checking ledger.open_for!
    res = chat_tool_stream.wait_for_approval(
        _FakeLedger(open_now=True), "run_code", "dig", "call-already-dead",
        timeout_seconds=5.0, is_aborted=lambda: True,
    )
    assert res == "aborted"

    # 2. Non-finite timeout_seconds and zero/NaN poll_interval never infinite-loop or busy-spin.
    res_expire = chat_tool_stream.wait_for_approval(
        _FakeLedger(), "run_code", "dig", "call-expire",
        timeout_seconds=0.05, poll_interval=0.0,
    )
    assert res_expire == "expired"
    res_nan_poll = chat_tool_stream.wait_for_approval(
        _FakeLedger(), "run_code", "dig", "call-nan-poll",
        timeout_seconds=0.05, poll_interval=float("nan"),
    )
    assert res_nan_poll == "expired"

    # 3. Multiple concurrent waiters on the SAME call_id BOTH wake on one notify_call_decision
    #    and clean up completely from _CALL_WAITERS.
    key = "call-shared-multi"
    ev1 = chat_tool_stream.register_call_waiter(key)
    ev2 = chat_tool_stream.register_call_waiter(key)
    try:
        chat_tool_stream.notify_call_decision(key)
        assert ev1.is_set() and ev2.is_set()
    finally:
        chat_tool_stream.unregister_call_waiter(key, ev1)
        chat_tool_stream.unregister_call_waiter(key, ev2)
    assert key not in chat_tool_stream._CALL_WAITERS

    # 4. cancel_call_waiters wakes a blocked approval wait immediately on abort.
    aborted_flag = {"v": False}
    result_box: list[str] = []

    def waiter() -> None:
        result_box.append(
            chat_tool_stream.wait_for_approval(
                _FakeLedger(), "run_code", "dig", "call-cancel-fast",
                timeout_seconds=30.0, poll_interval=1.0,
                is_aborted=lambda: aborted_flag["v"],
            )
        )

    t = threading.Thread(target=waiter, daemon=True)
    started = time.monotonic()
    t.start()
    time.sleep(0.05)
    aborted_flag["v"] = True
    woken = chat_tool_stream.cancel_call_waiters("call-cancel-fast")
    t.join(timeout=2.0)
    elapsed = time.monotonic() - started
    assert woken >= 1
    assert result_box == ["aborted"]
    assert elapsed < 0.8
    assert "call-cancel-fast" not in chat_tool_stream._CALL_WAITERS


def test_write_tool_chunk_handles_broken_pipe_missing_wfile_and_unserializable_payload():
    def ok_chunk(fid: str, model: str, delta: dict[str, Any], **kw: Any) -> str:
        return json.dumps({"id": fid, "model": model, "delta": delta, **kw}) + "\n"

    # 1. Missing wfile marks client_gone=True and returns False.
    early_missing = SimpleNamespace(completion_id="", client_gone=False, lock=threading.Lock())
    handler_missing = SimpleNamespace(_early_stream=early_missing, wfile=None)
    assert chat_tool_stream.write_tool_chunk(
        handler_missing, ok_chunk, iid="iid-1", model="m", payload={"phase": "started"},
    ) is False
    assert early_missing.client_gone is True

    # 2. Serialization failure in chunk() returns False WITHOUT setting client_gone=True.
    buf = io.BytesIO()
    early_ser = SimpleNamespace(completion_id="", client_gone=False, lock=threading.Lock())
    handler_ser = SimpleNamespace(_early_stream=early_ser, wfile=buf)

    def bad_chunk(*_a: Any, **_kw: Any) -> str:
        raise TypeError("cannot serialize object")

    assert chat_tool_stream.write_tool_chunk(
        handler_ser, bad_chunk, iid="iid-2", model="m", payload={"phase": "started"},
    ) is False
    assert early_ser.client_gone is False

    # 3. Broken pipe (OSError) marks client_gone=True and returns False.
    class _BrokenWFile:
        def write(self, _data: bytes) -> int:
            raise BrokenPipeError("client disconnected")

    early_pipe = SimpleNamespace(completion_id="", client_gone=False, lock=threading.Lock())
    handler_pipe = SimpleNamespace(_early_stream=early_pipe, wfile=_BrokenWFile())
    assert chat_tool_stream.write_tool_chunk(
        handler_pipe, ok_chunk, iid="iid-3", model="m", payload={"phase": "started"},
    ) is False
    assert early_pipe.client_gone is True


def test_cancel_mid_tool_call_prevents_execution_retries_and_emits_cancelled():
    # 1. Abort triggered during "started" SSE emission prevents backend.execute.
    aborted_a = {"v": False}
    backend_a = _StubBackend(lambda _n, _a: _ok_result("ran"))
    manifest = build_tool_turn_manifest(admitted_tools=("file_read",), user_run_command=False, mode="auto")
    agent_events: list[dict[str, Any]] = []

    def on_tool(evt: dict[str, Any]) -> None:
        if evt.get("phase") == "started":
            aborted_a["v"] = True

    turn_a = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend_a,
        permission_policy=_AllowPolicy(),
        on_tool_event=on_tool,
        on_agent_event=agent_events.append,
        is_aborted=lambda: aborted_a["v"],
    )
    steps_a = iter([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "file_read", "arguments": '{"path": "a.py"}'}},
                    {"id": "c2", "function": {"name": "file_read", "arguments": '{"path": "b.py"}'}},
                ],
            }
        },
        {"message": {"role": "assistant", "content": "Should not reach step 2."}},
    ])
    outcome_a = turn_a.run(lambda _m, _t: next(steps_a), system="sys", prompt="read")
    assert backend_a.calls == []
    assert [r.status for r in outcome_a.records] == ["aborted"]
    assert outcome_a.response_meta.get("aborted") is True
    assert any(
        ev.get("phase") == "failed" and ev.get("ok") is False and "cancelled" in str(ev.get("summary") or "").lower()
        for ev in agent_events
    )
    assert not any(ev.get("phase") == "completed" for ev in agent_events)

    # 2. Abort firing simultaneously as wait_for_approval returns "approved" prevents execution.
    aborted_b = {"v": False}
    backend_b = _StubBackend(lambda _n, _a: _ok_result("should-not-run"))
    policy_b = _AllowPolicy(allowed=False, source="unattended", call_id="call-race-1")

    def approving_then_aborting(_name: str, _cid: str, _dec: Any, _prev: dict[str, Any]) -> str:
        aborted_b["v"] = True
        return "approved"

    turn_b = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=backend_b,
        permission_policy=policy_b,
        wait_for_approval=approving_then_aborting,
        is_aborted=lambda: aborted_b["v"],
    )
    obs_b, _ = turn_b._run_tool_call(
        {"function": {"name": "file_read", "arguments": '{"path": "secret.txt"}'}},
        run_count=0,
    )
    assert "aborted" in obs_b
    assert backend_b.calls == []
    assert [r.status for r in turn_b.records] == ["aborted"]

    # 3. Abort flipping during attempt 0 of read-only tool prevents attempt 1 retry.
    aborted_c = {"v": False}
    attempts_c = {"n": 0}

    def failing_then_aborted(_name: str, _args: dict[str, Any]) -> Any:
        attempts_c["n"] += 1
        aborted_c["v"] = True
        raise TimeoutError("cancelled socket")

    turn_c = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=_StubBackend(failing_then_aborted),
        permission_policy=_AllowPolicy(),
        is_aborted=lambda: aborted_c["v"],
    )
    obs_c, _ = turn_c._run_tool_call(
        {"function": {"name": "file_read", "arguments": '{"path": "a.py"}'}},
        run_count=0,
    )
    assert attempts_c["n"] == 1
    assert "cancelled" in obs_c
    assert [r.status for r in turn_c.records] == ["aborted"]

    # 4. Abort during transient model error stops without retrying.
    aborted_d = {"v": False}
    model_attempts = {"n": 0}

    def transient_then_abort(_msgs: list[dict[str, Any]], _tools: Any) -> dict[str, Any]:
        model_attempts["n"] += 1
        aborted_d["v"] = True
        raise TimeoutError("request cancelled")

    turn_d = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=_StubBackend(lambda _n, _a: _ok_result()),
        permission_policy=_AllowPolicy(),
        is_aborted=lambda: aborted_d["v"],
    )
    outcome_d = turn_d.run(transient_then_abort, system="sys", prompt="hi")
    assert model_attempts["n"] == 1
    assert outcome_d.response_meta.get("aborted") is True


# --- 5. Strict bounded tool output truncation & exception redaction -----------


@pytest.mark.parametrize("limit", [-10, 0, 1, 10, 50, 200, 599, 600, 1000, 4000])
def test_clip_strictly_enforces_limit_across_all_boundaries(limit: int):
    manifest = build_tool_turn_manifest(admitted_tools=("file_read",), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=_StubBackend(lambda _n, _a: _ok_result()),
        permission_policy=_AllowPolicy(),
    )
    payload = "HEAD_MARKER " + ("M" * 9500) + " TAIL_MARKER"
    clipped, truncated = turn._clip(payload, limit)
    expected_cap = max(0, limit)
    assert truncated is True
    assert len(clipped) <= expected_cap
    if limit >= 600:
        assert "HEAD_MARKER" in clipped
        assert "TAIL_MARKER" in clipped
        assert "head & tail preserved" in clipped

    none_clipped, none_trunc = turn._clip(None, limit)
    assert none_clipped == ""
    assert none_trunc is False


def test_unexpected_tool_exception_is_clipped_and_secret_redacted():
    secret = "sk-proj-SUPERSECRETKEY1234567890abcd"

    def crashing_tool(_name: str, _args: dict[str, Any]) -> Any:
        raise RuntimeError(f"boom token={secret} " + ("Z" * 20_000))

    manifest = build_tool_turn_manifest(admitted_tools=("file_read",), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=_StubBackend(crashing_tool),
        permission_policy=_AllowPolicy(),
        budget=ChatToolBudget(model_output_chars=800),
    )
    obs, _ = turn._run_tool_call(
        {"function": {"name": "file_read", "arguments": '{"path": "a.py"}'}},
        run_count=0,
    )
    assert secret not in obs
    assert len(obs) < 1600  # clipped base error (<=800) + compact fallback hint
    assert len(turn.records) == 1
    assert turn.records[0].truncated is True
    assert secret not in turn.records[0].reason
    assert len(turn.records[0].reason) <= 240


def test_receipt_previews_bounded_for_oversized_custom_backend_results():
    def giant_backend(_name: str, _args: dict[str, Any]) -> ToolExecutionResult:
        return ToolExecutionResult(
            ok=True,
            output="X" * 50_000,
            backend="workspace",
            duration_ms=5,
            exit_code=0,
            stdout_preview="S" * 15_000,
            stderr_preview="E" * 15_000,
            reason="R" * 5_000,
        )

    manifest = build_tool_turn_manifest(admitted_tools=("file_read",), user_run_command=False, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=_StubBackend(giant_backend),
        permission_policy=_AllowPolicy(),
        budget=ChatToolBudget(model_output_chars=1000),
    )
    obs, _ = turn._run_tool_call(
        {"function": {"name": "file_read", "arguments": '{"path": "big.log"}'}},
        run_count=0,
    )
    assert len(obs) <= 1000
    assert len(turn.records) == 1
    rec = turn.records[0]
    assert rec.truncated is True
    assert len(rec.stdout_preview) <= 500
    assert len(rec.stderr_preview) <= 500
    assert len(rec.reason) <= 240


def test_run_code_alias_deduplicates_auto_verify():
    code = "def solve():\n    return 42\nprint(solve())"
    executed: list[str] = []

    def exec_fn(name: str, args: dict[str, Any]) -> ToolExecutionResult:
        executed.append(name)
        return _ok_result("42")

    manifest = build_tool_turn_manifest(admitted_tools=("run_code",), user_run_command=True, mode="auto")
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=[],
        backend=_StubBackend(exec_fn),
        permission_policy=_AllowPolicy(),
        budget=ChatToolBudget(max_runs=3, max_model_calls=4),
    )
    steps = iter([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "run_python", "arguments": json.dumps({"code": code})}},
                ],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": f"Result is 42:\n```python\n{code}\n```",
            }
        },
    ])
    outcome = turn.run(lambda _m, _t: next(steps), system="sys", prompt="solve")
    assert "42" in outcome.content
    assert executed == ["run_code"]  # Alias resolved & recorded hash -> auto-verify did not re-run!
