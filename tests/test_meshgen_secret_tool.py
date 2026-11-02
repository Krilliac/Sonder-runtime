"""The secret guard at the tool level: fail-closed set-up, the egress chokepoint, blocked candidates, the security note."""
from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters.meshgen import secret_sources
from sonder_runtime.bootstrap import mesh_agent_tools
from sonder_runtime.domain.meshgen.secret_guard import REDACTED, SecretGuard, SecretGuardUnavailable

from tests.test_meshgen_tool import FakeRunner, call, fake_runtime

VALUE = "canary-9f8e7d6c5b4a39281706"
GUARD = SecretGuard({"SONDER_CANARY_TOKEN": VALUE})


class LeakyRunner(FakeRunner):
    """A runner whose builds fail with the secret in the message, in the tail and in a part name."""

    def build_candidate(self, blender, code_path, out_dir, **kw):
        self.calls.append((code_path, out_dir, kw))
        return {"ok": False, "error": "ValueError: " + VALUE, "tail": ["Traceback", "ValueError: " + VALUE]}


class BlockedRunner(FakeRunner):
    def build_candidate(self, blender, code_path, out_dir, **kw):
        self.calls.append((code_path, out_dir, kw))
        return {"ok": False, "error": REDACTED, "secret_leak_blocked": True}


def test_a_guard_that_cannot_be_built_refuses_the_run_before_anything_happens(tmp_path, monkeypatch):
    def broken(env=None):
        raise SecretGuardUnavailable("cannot read the user environment from the registry (PermissionError)")

    monkeypatch.setattr(secret_sources, "load_guard", broken)
    runner = FakeRunner()
    rt = fake_runtime(tmp_path)
    with pytest.raises(mesh_agent_tools.SonderError, match="secret guard could not be initialised"):
        mesh_agent_tools.generate_mesh(brief="a cart", rt=rt, runner=runner)
    assert runner.calls == [] and rt.generated == []  # no model time, no build


def test_the_default_guard_is_built_from_the_host_sources(tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(secret_sources, "load_guard", lambda env=None: built.append(1) or GUARD)
    call(tmp_path, candidates=1, rounds=1)
    assert built == [1]


def test_an_error_that_carries_the_secret_never_reaches_the_payload(tmp_path):
    payload, _, _ = call(tmp_path, runner=LeakyRunner(), guard=GUARD, candidates=1, rounds=1)
    text = json.dumps(payload)
    assert VALUE not in text and REDACTED in text
    assert payload["security"]["secret_leak_blocked"] is True
    assert "SONDER_CANARY_TOKEN" not in text  # neither the name nor the value is ever reported


def test_a_blocked_candidate_is_flagged_and_the_report_carries_a_security_note(tmp_path):
    payload, _, _ = call(tmp_path, runner=BlockedRunner(), guard=GUARD, candidates=2, rounds=2)
    assert payload["ok"] is False
    assert all(item["secret_leak_blocked"] for item in payload["candidates"])
    assert "final" not in payload and "security" in payload
    assert VALUE not in json.dumps(payload)


def test_the_repair_prompt_after_a_block_carries_guidance_and_no_secret(tmp_path):
    seen = []
    rt = fake_runtime(tmp_path)
    original = rt._generate_text

    def spy(prompt, **kw):
        seen.append(prompt)
        return original(prompt, **kw)

    rt._generate_text = spy
    call(tmp_path, rt=rt, runner=BlockedRunner(), guard=GUARD, candidates=1, rounds=2)
    assert len(seen) == 2 and "looks like a credential" in seen[1] and VALUE not in seen[1]


def test_a_script_that_carries_the_secret_is_neither_written_nor_built(tmp_path):
    rt = fake_runtime(tmp_path)
    rt._generate_text = lambda prompt, **kw: "```python\nimport bpy\n\ndef build():\n    x = '%s'\n```" % VALUE
    runner = FakeRunner()
    payload, _, _ = call(tmp_path, rt=rt, runner=runner, guard=GUARD, candidates=1, rounds=1)
    assert runner.calls == []
    assert payload["candidates"][0]["secret_leak_blocked"] is True and "security" in payload
    assert VALUE not in json.dumps(payload)
    assert not [p for p in tmp_path.rglob("*.py") if VALUE in p.read_text(encoding="utf-8", errors="ignore")]


def test_critic_text_that_carries_the_secret_is_redacted_and_noted(tmp_path):
    rt = fake_runtime(tmp_path)
    rt._vision_analyze_impl = lambda path, prompt, **kw: json.dumps(
        {"score": 3, "issues": ["the label says " + VALUE]})
    payload, _, _ = call(tmp_path, rt=rt, guard=GUARD, candidates=1, rounds=1)
    text = json.dumps(payload)
    assert VALUE not in text and payload["security"]["secret_leak_blocked"] is True


def test_clean_runs_carry_no_security_note(tmp_path):
    payload, _, _ = call(tmp_path, guard=GUARD, candidates=1, rounds=1)
    assert payload["ok"] is True and "security" not in payload


def test_the_payload_carries_structured_fields_only(tmp_path):
    payload, _, _ = call(tmp_path, guard=GUARD, candidates=1, rounds=1)
    allowed_top = {"ok", "name", "output_dir", "blender", "vision", "elapsed_s", "winner", "candidates", "final",
                   "sandbox", "warning", "registry", "security"}
    assert set(payload) <= allowed_top
    for candidate in payload["candidates"]:
        for attempt in candidate["attempts"]:
            assert set(attempt) <= {"tier", "round", "status", "tris", "score", "issues", "seconds", "error",
                                    "stuck", "escalated_to", "secret_leak_blocked"}
