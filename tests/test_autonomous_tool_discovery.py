"""Tests for autonomous cross-tool discovery, dynamic loading, plan-mode reads, and scratchpad index injection."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import permission_modes as pm
import pytest
import server

from sonder_runtime.adapters.chat_run_backend import ChatRunBackend
from sonder_runtime.adapters.persistence.scratchpad_store import ScratchpadStore
from sonder_runtime.application.chat import tool_manifest as tm
from sonder_runtime.application.chat.tool_loop import (
    ChatToolTurn,
    build_tool_turn_manifest,
)
from sonder_runtime.application.memory.scratchpad_context import frame_scratchpad_index
from sonder_runtime.bootstrap import chat_tools_wiring
from sonder_runtime.bootstrap import scratchpad as scratchpad_bootstrap
from sonder_runtime.domain.agents.tool_help import match_catalog_tools, tool_help_text
from sonder_runtime.domain.chat_tools import (
    ChatToolBudget,
    catalog_entry_to_ollama_tool,
    chat_tools_payload,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_permission_state():
    saved_state = dict(pm._STATE)
    saved_scopes = dict(pm._SCOPED_MODES)
    saved_loaded = pm._LOADED
    yield
    with pm._LOCK:
        pm._STATE.clear()
        pm._STATE.update(saved_state)
        pm._SCOPED_MODES.clear()
        pm._SCOPED_MODES.update(saved_scopes)
        pm._LOADED = saved_loaded


def _sample_catalog() -> dict[str, dict[str, Any]]:
    return {
        "repo_status": {
            "description": "Inspect git working tree status and staged files.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "root": {"type": "string", "default": "."},
                    "extra_roots": {"type": "string"},
                    "token": {"type": "string"},
                },
            },
        },
        "file_find": {
            "description": "Find files by glob pattern in the repository.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "root": {"type": "string"}},
                "required": ["query"],
            },
        },
        "document_extract": {
            "description": "Extract text and tables from DOCX, XLSX, PPTX, HTML, SVG, or SRT files.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        "elevate": {
            "description": "Elevate session privileges.",
            "input_schema": {"type": "object", "properties": {}},
        },
        "admin_set_account": {
            "description": "Modify admin account settings.",
            "input_schema": {"type": "object", "properties": {}},
        },
    }


def test_tool_search_multiword_and_description_matching():
    cat = _sample_catalog()
    assert "repo_status" in match_catalog_tools(query="check git status", catalog=cat)
    assert "document_extract" in match_catalog_tools(query="extract docx tables", catalog=cat)
    out = tool_help_text(query="extract docx tables", catalog=cat)
    assert out.startswith("matching tools:")
    assert "document_extract" in out

    # Authority arguments are stripped when converting catalog entries to OpenAI/Ollama schemas.
    ollama_schema = catalog_entry_to_ollama_tool("repo_status", cat["repo_status"])
    props = ollama_schema["function"]["parameters"]["properties"]
    assert "root" in props
    assert "extra_roots" not in props
    assert "token" not in props


def test_tool_search_allowed_by_real_permission_modes_and_respects_deny_rule(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _n: None)
    manifest = build_tool_turn_manifest(
        admitted_tools=["tool_search"],
        user_run_command=False,
        mode="plan",
        dynamic_discovery=True,
    )
    backend = ChatRunBackend(
        dispatch_agent_tool=lambda *_a, **_kw: "ok",
        catalog_provider=_sample_catalog,
        permission_policy=pm,
        blocked_tools=frozenset({"elevate"}),
    )

    for mode in (pm.PLAN, pm.MANUAL, pm.ACCEPT_EDITS, pm.AUTO):
        pm.set_mode(mode)
        turn = ChatToolTurn(
            manifest=manifest,
            tools_payload=chat_tools_payload(allowed_names=["tool_search"]),
            backend=backend,
            permission_policy=pm,
            blocked_tools=frozenset({"elevate"}),
            allow_dynamic_catalog=True,
        )
        obs, _ = turn._run_tool_call(
            {"function": {"name": "tool_search", "arguments": {"query": "git"}}},
            run_count=0,
        )
        assert "ERROR: HOST POLICY" not in obs, (mode, obs)
        assert "repo_status" in obs

    # An explicit deny rule on tool_search still wins (never bypassed by fallback to tool_help).
    monkeypatch.setattr(
        pm,
        "_rule_lookup",
        lambda n: {"tool": n, "action": "deny", "reason": "operator denied"} if n == "tool_search" else None,
    )
    pm.set_mode(pm.AUTO)
    turn_denied = ChatToolTurn(
        manifest=manifest,
        tools_payload=chat_tools_payload(allowed_names=["tool_search"]),
        backend=backend,
        permission_policy=pm,
        allow_dynamic_catalog=True,
    )
    obs_denied, _ = turn_denied._run_tool_call(
        {"function": {"name": "tool_search", "arguments": {"query": "git"}}},
        run_count=0,
    )
    assert "HOST POLICY" in obs_denied


def test_tool_search_dynamically_loads_schemas_and_makes_tools_callable(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _n: None)
    pm.set_mode(pm.MANUAL)
    executed: list[tuple[str, dict[str, Any]]] = []
    seen_tool_names_per_step: list[set[str]] = []

    manifest = build_tool_turn_manifest(
        admitted_tools=["tool_search", "file_read"],
        user_run_command=True,
        mode="manual",
        dynamic_discovery=True,
    )
    backend = ChatRunBackend(
        dispatch_agent_tool=lambda name, args, **_kw: executed.append((name, dict(args))) or f"ran:{name}",
        catalog_provider=_sample_catalog,
        permission_policy=pm,
        blocked_tools=frozenset({"elevate"}),
    )
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=chat_tools_payload(allowed_names=["tool_search", "file_read"]),
        backend=backend,
        permission_policy=pm,
        blocked_tools=frozenset({"elevate"}),
        allow_dynamic_catalog=True,
        budget=ChatToolBudget(max_model_calls=5, max_tool_calls=10),
    )

    replies = iter([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "tool_search", "arguments": {"query": "git"}}},
                ],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c2", "function": {"name": "repo_status", "arguments": {"root": "."}}},
                ],
            }
        },
        {"message": {"role": "assistant", "content": "Working tree clean."}},
    ])

    def step(_msgs, tools_payload):
        if tools_payload:
            seen_tool_names_per_step.append({t["function"]["name"] for t in tools_payload})
        return next(replies)

    outcome = turn.run(step, system="sys", prompt="check git status", history=None)
    assert outcome.content == "Working tree clean."
    assert executed == [("repo_status", {"root": "."})]
    assert "repo_status" not in seen_tool_names_per_step[0]
    assert "repo_status" in seen_tool_names_per_step[1]


def test_direct_catalog_auto_admission_allows_permitted_and_blocks_operator_tools(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _n: None)
    pm.set_mode(pm.MANUAL)
    executed: list[str] = []

    manifest = build_tool_turn_manifest(
        admitted_tools=["tool_search", "file_read"],
        user_run_command=True,
        mode="manual",
        dynamic_discovery=True,
    )
    backend = ChatRunBackend(
        dispatch_agent_tool=lambda name, _args, **_kw: executed.append(name) or f"ran:{name}",
        catalog_provider=_sample_catalog,
        permission_policy=pm,
        blocked_tools=frozenset({"elevate"}),
    )
    turn = ChatToolTurn(
        manifest=manifest,
        tools_payload=chat_tools_payload(allowed_names=["tool_search", "file_read"]),
        backend=backend,
        permission_policy=pm,
        blocked_tools=frozenset({"elevate"}),
        allow_dynamic_catalog=True,
    )

    # Permitted non-core catalog tool auto-admits and runs.
    obs_ok, _ = turn._run_tool_call(
        {"function": {"name": "file_find", "arguments": {"query": "*.py"}}},
        run_count=0,
    )
    assert obs_ok == "ran:file_find"
    assert executed == ["file_find"]

    # Blocked operator tool and admin_* tool are NEVER admitted or executed.
    obs_elevate, _ = turn._run_tool_call(
        {"function": {"name": "elevate", "arguments": {}}},
        run_count=0,
    )
    assert "restricted to operator control surfaces" in obs_elevate

    obs_admin, _ = turn._run_tool_call(
        {"function": {"name": "admin_set_account", "arguments": {}}},
        run_count=0,
    )
    assert "ran:admin_set_account" not in obs_admin
    assert executed == ["file_find"]


def test_plan_mode_chat_turns_enable_readonly_tools_and_dynamic_discovery_only(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _n: None)
    monkeypatch.setattr(chat_tools_wiring, "_model_supports_tools", lambda *_a, **_kw: True)
    pm.set_mode(pm.PLAN)
    handler_stub = SimpleNamespace(
        _early_stream=SimpleNamespace(completion_id="iid-plan", client_gone=False, lock=threading.Lock())
    )

    http_turn = chat_tools_wiring.build_http_chat_tool_turn(
        server=server,
        permission_policy=pm,
        handler=handler_stub,
        chunk_fn=lambda *_a, **_kw: "",
        model="qwen",
        project="",
        stream=False,
        authorized_tools=True,
    )
    assert http_turn is not None
    assert http_turn.allow_dynamic_catalog is True
    assert http_turn.manifest.user_run_command is False
    assert http_turn.manifest.dynamic_discovery is True
    admitted = {t.name for t in http_turn.manifest.tools}
    assert {"file_read", "scratchpad_read", "tool_search", "math_eval"} <= admitted
    assert {"file_write", "file_edit", "run_code", "scratchpad_write", "scratchpad_delete"}.isdisjoint(admitted)

    # Dynamic manifest guidance keeps machine neutrality and single-section invariant.
    rendered = http_turn.manifest.render()
    assert "\n\n" not in rendered
    assert "tool_search(query=...)" in rendered
    assert tm.PROACTIVE_TOOL_LINE in rendered
    assert "/run" not in rendered

    mcp_turn = chat_tools_wiring.maybe_open_mcp_chat_tool_turn(
        server=server,
        permission_policy=pm,
        model="qwen",
        cloud=False,
        project="",
    )
    assert mcp_turn is not None
    mcp_admitted = {t.name for t in mcp_turn.manifest.tools}
    assert {"file_read", "scratchpad_read", "tool_search"} <= mcp_admitted
    assert {"file_write", "run_code", "scratchpad_write"}.isdisjoint(mcp_admitted)


def test_scratchpad_index_context_injection_and_cloud_privacy_boundary(tmp_path, monkeypatch):
    assert frame_scratchpad_index([]) == ""

    store = ScratchpadStore(tmp_path)
    store.write("local", "architecture-plan", "# Plan\nline 2\n", tags=["design", "w4"])
    store.write("local", "debug-log", "checked bug <script>alert(1)</script>\n")

    monkeypatch.setattr(scratchpad_bootstrap, "get_store", lambda **_kw: store)
    monkeypatch.setattr(scratchpad_bootstrap, "resolve_owner", lambda *_a, **_kw: "local")

    block = scratchpad_bootstrap.scratchpad_index_block()
    assert "OWNER SCRATCHPAD INDEX" in block
    assert "architecture-plan [design, w4]" in block
    assert "# Plan" in block
    assert "debug-log" in block
    assert "<script>" not in block
    assert "END OWNER SCRATCHPAD INDEX" in block

    # Never leaks to cloud turns.
    assert scratchpad_bootstrap.augment_with_scratchpad_index("BASE", cloud=True) == "BASE"
    local_aug = scratchpad_bootstrap.augment_with_scratchpad_index("BASE", cloud=False)
    assert "OWNER SCRATCHPAD INDEX" in local_aug

    # Local _build_system includes the scratchpad index while cloud _build_system omits it.
    local_sys = server._build_system("REQ", False, "", model="qwen", cloud=False)
    assert "OWNER SCRATCHPAD INDEX" in local_sys
    cloud_sys = server._build_system("REQ", False, "", model="cloud-model", cloud=True)
    assert "OWNER SCRATCHPAD INDEX" not in cloud_sys

    # TurnToolManifest remains strictly last even when scratchpad index is present.
    from sonder_runtime.adapters import chat_tool_manifest

    composed = chat_tool_manifest.chat_system(local_sys, "hi", file_guidance=False)
    assert composed.index("OWNER SCRATCHPAD INDEX") < composed.index(tm.HEADER)
