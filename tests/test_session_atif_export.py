"""ATIF-v1.7 trajectory export of Sonder sessions (Harbor / Terminal-Bench).

Covers: default-format parity against a golden captured from the pre-change
code, ATIF schema shape (required fields per the Harbor models), tool-call /
observation linkage by id, embedded subagent trajectories, per-step metrics
and ``llm_call_count``, and redaction.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.atif import (
    ATIF_SCHEMA_VERSION,
    AtifAgent,
    AtifExportError,
    interaction_turns_to_atif,
    session_events_to_atif,
    validate_atif,
)
from sonder_runtime.application.session.capture import CapturedTool, SessionCaptureService
from sonder_runtime.application.session.http_facade import HttpSessionFacade
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.interfaces.http.facades.session import dispatch_session_route
from tests.fixtures import session_export_parity as parity

GOLDEN = Path(__file__).parent / "fixtures" / "session_export_parity_golden.json"


# ---------------------------------------------------------------- parity ----

def test_default_exports_are_byte_identical_to_pre_change_golden(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "_DB_PATH", server._DB_PATH)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    current = parity.default_exports(tmp_path)
    assert sorted(current) == sorted(golden)
    assert len(golden) == 16
    for key in sorted(golden):
        assert current[key] == golden[key], key


@pytest.mark.parametrize("fmt", [None, "", "sonder", "SONDER"])
def test_default_format_spellings_return_the_unchanged_envelope(tmp_path, fmt):
    facade = HttpSessionFacade(parity.build_session_repo(tmp_path / "s.db"))
    plain = facade.export(parity.PARENT)
    chosen = facade.export(parity.PARENT, format=fmt)
    assert chosen.status_code == plain.status_code == 200
    assert json.dumps(chosen.body, sort_keys=True) == json.dumps(plain.body, sort_keys=True)
    # Pre-existing: the envelope's own "schema" is overridden by to_dict()'s.
    assert chosen.body["schema"] == "sonder.session-export.v1"


def test_unknown_format_is_rejected_not_silently_defaulted(tmp_path):
    facade = HttpSessionFacade(parity.build_session_repo(tmp_path / "s.db"))
    result = facade.export(parity.PARENT, format="atif-v2")
    assert result.status_code == 400
    assert result.body == {"error": "invalid_session_export"}


# ---------------------------------------------------------------- helpers ---

def _atif(tmp_path, **kwargs):
    facade = HttpSessionFacade(parity.build_session_repo(tmp_path / "s.db"),
                               agent_version="9.9.9-test", **kwargs)
    result = facade.export(parity.PARENT, format="atif")
    assert result.status_code == 200, result.body
    return result.body


def _step_with_call(doc, call_id):
    return next(step for step in doc["steps"]
                if any(call["tool_call_id"] == call_id for call in step.get("tool_calls", ())))


# ---------------------------------------------------------- schema shape ----

def test_atif_document_has_every_required_field_and_validates(tmp_path):
    doc = _atif(tmp_path)
    assert validate_atif(doc) == []
    assert doc["schema_version"] == ATIF_SCHEMA_VERSION == "ATIF-v1.7"
    assert doc["session_id"] == doc["trajectory_id"] == parity.PARENT
    assert doc["agent"] == {"name": "sonder", "version": "9.9.9-test", "model_name": "qwen3:8b"}
    assert [step["step_id"] for step in doc["steps"]] == list(range(1, len(doc["steps"]) + 1))
    for step in doc["steps"]:
        assert step["source"] in {"system", "user", "agent"}
        assert isinstance(step["message"], str)
        assert step["timestamp"].endswith("Z")
    assert doc["steps"][0] == {**doc["steps"][0], "source": "system", "message": "You are Sonder."}
    # Same system prompt on the second request is not repeated.
    assert sum(step["source"] == "system" and step["message"] == "You are Sonder."
               for step in doc["steps"]) == 1
    assert doc["final_metrics"]["total_steps"] == len(doc["steps"])
    assert doc["extra"]["integrity_valid"] is True
    assert doc["extra"]["truncated"] is False


def test_atif_route_serves_the_document(tmp_path):
    facade = HttpSessionFacade(parity.build_session_repo(tmp_path / "s.db"))
    routed = dispatch_session_route(facade, "/v1/sessions/%s/export" % parity.PARENT,
                                    query={"format": ["atif"]})
    assert routed.status_code == 200
    assert routed.body["schema_version"] == "ATIF-v1.7"
    assert validate_atif(routed.body) == []


def test_validator_is_not_vacuous():
    base = {"schema_version": "ATIF-v1.7", "agent": {"name": "a", "version": "1"},
            "steps": [{"step_id": 1, "source": "agent", "message": "",
                       "tool_calls": [{"tool_call_id": "c1", "function_name": "f",
                                       "arguments": {}}],
                       "observation": {"results": [{"source_call_id": "c1", "content": "x"}]}}]}
    assert validate_atif(base) == []
    broken = {
        "missing agent": lambda d: d.pop("agent"),
        "missing steps": lambda d: d.update(steps=[]),
        "bad version": lambda d: d.update(schema_version="ATIF-v9"),
        "undeclared field": lambda d: d.update(bogus=1),
        "step id gap": lambda d: d["steps"][0].update(step_id=2),
        "bad source": lambda d: d["steps"][0].update(source="tool"),
        "no message": lambda d: d["steps"][0].pop("message"),
        "dangling source_call_id": lambda d: d["steps"][0]["observation"]["results"][0]
        .update(source_call_id="c9"),
        "args not object": lambda d: d["steps"][0]["tool_calls"][0].update(arguments="x"),
        "user with tool_calls": lambda d: d["steps"][0].update(source="user"),
        "zero calls with metrics": lambda d: d["steps"][0].update(llm_call_count=0,
                                                                  metrics={"prompt_tokens": 1}),
        "bad timestamp": lambda d: d["steps"][0].update(timestamp="yesterday"),
        "unresolvable ref": lambda d: d["steps"][0]["observation"]["results"][0]
        .update(subagent_trajectory_ref=[{"trajectory_id": "nope"}]),
        "embedded without id": lambda d: d.update(subagent_trajectories=[
            {"schema_version": "ATIF-v1.7", "agent": {"name": "a", "version": "1"},
             "steps": [{"step_id": 1, "source": "user", "message": "hi"}]}]),
    }
    for label, mutate in broken.items():
        doc = copy.deepcopy(base)
        mutate(doc)
        assert validate_atif(doc), label


# --------------------------------------------------------------- linkage ----

def test_tool_calls_and_observations_are_linked_by_id(tmp_path):
    doc = _atif(tmp_path)
    step = _step_with_call(doc, "call-1")
    assert step["source"] == "agent"
    names = {call["tool_call_id"]: call["function_name"] for call in step["tool_calls"]}
    assert names == {"call-1": "directory_tree", "call-2": "run_code"}
    results = step["observation"]["results"]
    linked = {result.get("source_call_id") for result in results}
    assert linked == {"call-1"}
    ok = next(result for result in results if result["source_call_id"] == "call-1")
    assert ok["content"] == '["a.py","b.py"]'
    assert ok["extra"]["status"] == "completed"
    # call-2's failure observation was retention-redacted: a placeholder step
    # records it, and no linkage is invented for it.
    placeholder = next(s for s in doc["steps"]
                       if s.get("extra", {}).get("privacy_class") == "private")
    assert placeholder["message"] == "[REDACTED]"
    assert "observation" not in placeholder
    assert step["tool_calls"][1]["arguments"] == {"cmd": "cat secrets"}


def test_real_capture_writer_shapes_link(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "cap.db", max_read_limit=1000)
    SessionCaptureService(repo).capture_turn(
        "cap", "t1", ModelRequest("read a.py", tier="code", system="sys"),
        request_id="r1", user_message="read a.py",
        tools=(CapturedTool("k1", "file_read", {"path": "a.py"}, {"text": "print(1)"}),
               CapturedTool("k2", "file_read", {"path": "b.py"}, "missing")),
        model_response="a.py prints 1",
    )
    doc = HttpSessionFacade(repo).export("cap", format="atif").body
    assert validate_atif(doc) == []
    sources = [step["source"] for step in doc["steps"]]
    assert sources == ["system", "user", "agent", "agent"]
    tool_step = doc["steps"][2]
    assert [c["tool_call_id"] for c in tool_step["tool_calls"]] == ["k1", "k2"]
    assert tool_step["tool_calls"][0]["arguments"] == {"path": "a.py"}
    assert [r["source_call_id"] for r in tool_step["observation"]["results"]] == ["k1", "k2"]
    assert doc["steps"][3]["message"] == "a.py prints 1"


def test_tool_failure_is_an_error_observation():
    records = _records([
        ("tool.call", {"content": "{}", "call_id": "x1", "name": "run_code"}),
        ("tool.failed", {"call_id": "x1", "error_code": "DEADLINE_EXCEEDED"}),
    ])
    doc = session_events_to_atif(records, session_id="s")
    result = doc["steps"][0]["observation"]["results"][0]
    assert result["source_call_id"] == "x1"
    assert result["content"] == "ERROR: DEADLINE_EXCEEDED"
    assert result["extra"]["status"] == "failed"
    assert validate_atif(doc) == []


def test_orphan_result_is_kept_unlinked():
    records = _records([("tool.result", {"content": "late", "call_id": "ghost"})])
    doc = session_events_to_atif(records, session_id="s")
    step = doc["steps"][0]
    assert step["llm_call_count"] == 0 and "tool_calls" not in step
    result = step["observation"]["results"][0]
    assert "source_call_id" not in result
    assert result["extra"]["unlinked_call_id"] == "ghost"
    assert validate_atif(doc) == []


# --------------------------------------------------------------- metrics ----

def test_model_calls_carry_usage_timing_and_llm_call_count(tmp_path):
    doc = _atif(tmp_path)
    reply = next(s for s in doc["steps"] if s["message"].startswith("Found a.py"))
    assert reply["llm_call_count"] == 1
    assert reply["model_name"] == "qwen3:8b"
    assert reply["metrics"]["prompt_tokens"] == 120
    assert reply["metrics"]["completion_tokens"] == 30
    assert reply["metrics"]["extra"]["provider_duration_ms"] == 1000
    assert reply["metrics"]["extra"]["providers"] == ["ollama"]
    assert reply["extra"]["request_id"] == "r1"
    failed = next(s for s in doc["steps"] if s.get("extra", {}).get("sonder_event_type")
                  == "model.failed")
    assert failed["extra"]["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert failed["extra"]["provider_failures"] == [
        {"attempt_id": "a2", "error_code": "DEPENDENCY_UNAVAILABLE"}]
    assert "metrics" not in failed and "llm_call_count" not in failed
    error = next(s for s in doc["steps"] if s["source"] == "system" and s["message"].startswith("error"))
    assert error["message"] == "error: DEPENDENCY_UNAVAILABLE"
    final = doc["final_metrics"]
    # Parent only: subagent usage stays in the embedded trajectory.
    assert final["total_prompt_tokens"] == 120
    assert final["total_completion_tokens"] == 30


# ------------------------------------------------------------- subagents ----

def test_linked_child_session_is_embedded_and_referenced(tmp_path):
    doc = _atif(tmp_path)
    assert validate_atif(doc) == []
    [child] = doc["subagent_trajectories"]
    assert child["trajectory_id"] == parity.CHILD
    assert child["schema_version"] == "ATIF-v1.7"
    assert [s["step_id"] for s in child["steps"]] == [1, 2]
    assert child["steps"][1]["metrics"] == {"prompt_tokens": 50, "completion_tokens": 9,
                                            "extra": {"provider_duration_ms": 1000,
                                                      "providers": ["ollama"]}}
    assert child["agent"]["model_name"] == "qwen3:4b"
    delegation = _step_with_call(doc, "subagent-c1")
    assert delegation["llm_call_count"] == 0 and "metrics" not in delegation
    assert delegation["tool_calls"][0]["arguments"] == {"subagent_id": "c1", "role": "reviewer"}
    result = delegation["observation"]["results"][0]
    assert result["source_call_id"] == "subagent-c1"
    assert result["content"] == "completed"
    assert result["subagent_trajectory_ref"] == [
        {"trajectory_id": parity.CHILD, "session_id": parity.CHILD}]


def test_unresolvable_subagent_is_kept_without_a_dangling_ref():
    records = _records([("subagent.spawned", {"subagent_id": "gone"}),
                        ("subagent.failed", {"subagent_id": "gone", "error_code": "CANCELLED"})])
    doc = session_events_to_atif(records, session_id="s", load_subagent=lambda _id: None)
    result = doc["steps"][0]["observation"]["results"][0]
    assert "subagent_trajectory_ref" not in result
    assert result["extra"]["subagent_resolved"] is False
    assert result["content"] == "failed: CANCELLED"
    assert "subagent_trajectories" not in doc
    assert validate_atif(doc) == []


def test_subagent_cycles_are_not_followed():
    def loader(subagent_id):
        return ("s", _records([("subagent.spawned", {"subagent_id": "s"})], session="s"))

    records = _records([("subagent.spawned", {"subagent_id": "s"})], session="s")
    doc = session_events_to_atif(records, session_id="s", load_subagent=loader)
    assert "subagent_trajectories" not in doc
    assert validate_atif(doc) == []


def test_embedded_subagents_are_capped_across_all_depths():
    from sonder_runtime.application.session.atif import MAX_EMBEDDED_SUBAGENTS

    calls = []

    def loader(subagent_id):
        # Every child delegates to two more: unbounded breadth without a cap.
        calls.append(subagent_id)
        return (subagent_id, _records([
            ("user.message", {"content": "work " + subagent_id}),
            ("subagent.spawned", {"subagent_id": subagent_id + ".a"}),
            ("subagent.spawned", {"subagent_id": subagent_id + ".b"}),
        ], session=subagent_id))

    fanout = MAX_EMBEDDED_SUBAGENTS + 5
    records = _records([("subagent.spawned", {"subagent_id": "c%02d" % i}) for i in range(fanout)])
    doc = session_events_to_atif(records, session_id="root", load_subagent=loader)
    assert len(calls) == MAX_EMBEDDED_SUBAGENTS

    def embedded(trajectory):
        return sum(1 + embedded(sub) for sub in trajectory.get("subagent_trajectories", ()))

    assert embedded(doc) == MAX_EMBEDDED_SUBAGENTS
    exhausted = [s for s in doc["steps"]
                 if s["observation"]["results"][0]["extra"].get("subagent_budget_exhausted")]
    assert exhausted and all("subagent_trajectory_ref" not in s["observation"]["results"][0]
                             for s in exhausted)
    assert validate_atif(doc) == []


def test_only_spec_enum_values_bypass_redaction():
    records = _records([("user.message", {"content": "hi"})])
    doc = session_events_to_atif(
        records, session_id="s",
        extra={"source": "password: swordfish99", "schema_version": "api_key=sk-abcdef0123456789abcdef"},
    )
    assert doc["steps"][0]["source"] == "user"
    assert doc["schema_version"] == ATIF_SCHEMA_VERSION
    text = json.dumps(doc)
    assert "swordfish99" not in text and "sk-abcdef0123456789abcdef" not in text


# ------------------------------------------------------------- redaction ----

def test_atif_never_contains_secrets(tmp_path):
    doc = _atif(tmp_path)
    text = json.dumps(doc)
    assert parity.SECRET not in text
    assert parity.PASSWORD not in text
    step = _step_with_call(doc, "call-1")
    assert step["tool_calls"][0]["arguments"] == {"password": "[REDACTED]", "path": "."}
    assert step["tool_calls"][0]["extra"]["arguments_redaction_requoted"] is True
    user = next(s for s in doc["steps"] if s["source"] == "user")
    assert "[REDACTED]" in user["message"]
    # Linkage survives the redaction pass.
    assert validate_atif(doc) == []


def test_redaction_pass_scrubs_secrets_the_store_still_holds():
    records = _records([
        ("user.message", {"content": "use Bearer abcdefghijklmnop123"}),
        ("tool.call", {"content": json.dumps({"token": "tok-9999-secret", "q": "x"}),
                       "call_id": "c", "name": "web_fetch"}),
        ("tool.result", {"content": "password: swordfish99", "call_id": "c"}),
    ])
    doc = session_events_to_atif(records, session_id="s")
    text = json.dumps(doc)
    for secret in ("abcdefghijklmnop123", "tok-9999-secret", "swordfish99"):
        assert secret not in text
    assert validate_atif(doc) == []


def test_empty_session_is_an_explicit_error(tmp_path):
    with pytest.raises(AtifExportError):
        session_events_to_atif((), session_id="s")
    repo = SQLiteSessionRepository(tmp_path / "e.db", max_read_limit=10)
    repo.append("only-bookkeeping", "session.started", {"status": "active"})
    result = HttpSessionFacade(repo).export("only-bookkeeping", format="atif")
    assert result.status_code == 409
    assert result.body == {"error": "session_atif_unavailable"}


# -------------------------------------------------- MCP interaction path ----

def test_mcp_session_export_atif_from_interactions(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "_DB_PATH", server._DB_PATH)
    parity.build_memory_db(server, tmp_path / "mem.db")
    doc = json.loads(server.session_export("S-parity", format="atif"))
    assert validate_atif(doc) == []
    assert doc["session_id"] == "S-parity"
    assert doc["agent"]["name"] == "sonder" and doc["agent"]["version"]
    assert [s["source"] for s in doc["steps"]] == ["user", "agent", "user", "agent"]
    assert doc["steps"][1]["metrics"]["prompt_tokens"] == 11
    assert doc["steps"][1]["metrics"]["completion_tokens"] == 3
    assert doc["steps"][1]["extra"]["tier"] == "code"
    assert "metrics" not in doc["steps"][3]
    assert doc["extra"]["title"] == "parity demo"
    text = json.dumps(doc)
    assert parity.SECRET not in text and parity.PASSWORD not in text
    tail = json.loads(server.session_export("S-parity", limit=1, format="ATIF"))
    assert [s["message"] for s in tail["steps"]][0] == "second"
    from sonder_runtime.domain.common.errors import InvalidInput

    with pytest.raises(InvalidInput, match="unknown session_export format"):
        server.session_export("S-parity", format="xml")
    assert server.session_export("S-parity", format="text") == server.session_export("S-parity")


def test_interaction_turns_validate_with_timestamps():
    doc = interaction_turns_to_atif(
        [{"id": "i", "task": "t", "response": "r", "ts": "2026-09-01 10:00:00"}],
        session_id="x", agent=AtifAgent(version="1"))
    assert doc["steps"][0]["timestamp"] == "2026-09-01T10:00:00Z"
    assert validate_atif(doc) == []


# ----------------------------------------------------------------- utils ----

def _records(items, session="s"):
    from sonder_runtime.application.session.query_export import SessionEventRecord

    return tuple(
        SessionEventRecord(session, index, "e%d" % index, kind,
                           "2026-09-01T00:00:%02dZ" % index, payload, None, "h%d" % index)
        for index, (kind, payload) in enumerate(items, start=1)
    )
