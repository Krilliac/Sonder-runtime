"""``mesh_generate`` registration, input handling and end-to-end wiring with fakes."""
from __future__ import annotations

import json
import types

import pytest

from sonder_runtime.bootstrap import agent_help_tools, mesh_agent_tools

GOOD = "```python\nimport bpy\n\ndef build():\n    pass\n```"


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class FakeAssetgen:
    def __init__(self, root):
        self.root = str(root)

    def workspace_root(self):
        return self.root

    def default_asset_root(self):
        return self.root + "/artifacts/generated"


class FakeRunner:
    """Stands in for blender_runner: finds 'blender' and writes real files."""

    def __init__(self, located="blender", fail=False):
        self.located = located
        self.fail = fail
        self.calls = []

    def locate_blender(self):
        return self.located

    def build_candidate(self, blender, code_path, out_dir, **kw):
        from pathlib import Path
        self.calls.append((code_path, out_dir, kw))
        if self.fail:
            return {"ok": False, "error": "boom"}
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        files = {"model": out / (kw["name"] + ".glb"), "sheet": out / (kw["name"] + "_sheet.png"),
                 "view": out / (kw["name"] + "_view.png")}
        for path in files.values():
            path.write_bytes(b"x")
        return {"ok": True, "tris": 120, "within_budget": True, "size": [1, 1, 1],
                "files": {k: str(v) for k, v in files.items()}}


def fake_runtime(tmp_path, vision_error=None, score=9):
    rt = types.SimpleNamespace()
    rt.assetgen = FakeAssetgen(tmp_path)
    rt.TIERS = {"general": "m1", "code": "m2", "fast": "m3"}
    rt.generated = []
    rt.vision_calls = []

    def generate(prompt, tier="fast", **kw):
        rt.generated.append((tier, kw))
        return GOOD

    def vision(path, prompt, **kw):
        rt.vision_calls.append((path, kw))
        if vision_error:
            raise RuntimeError(vision_error)
        return json.dumps({"score": score, "recognizable": True, "issues": []})

    rt._generate_text = generate
    rt._vision_analyze_impl = vision
    return rt


def call(tmp_path, runner=None, rt=None, **kw):
    runner = runner or FakeRunner()
    rt = rt or fake_runtime(tmp_path)
    kw.setdefault("brief", "a handcart")
    return mesh_agent_tools.generate_mesh(rt=rt, runner=runner, **kw), runner, rt


def test_register_exposes_mesh_generate_and_records_the_call(tmp_path, monkeypatch):
    recorded = []
    mcp = FakeMcp()
    mesh_agent_tools.register(mcp, lambda name, args, **kw: recorded.append((name, kw["ok"])))
    assert set(mcp.tools) == {"mesh_generate"}
    monkeypatch.setattr(mesh_agent_tools.blender_runner, "locate_blender", lambda env=None: None)
    monkeypatch.delenv("SONDER_BLENDER", raising=False)
    payload = json.loads(mcp.tools["mesh_generate"]("a cart"))
    assert payload["ok"] is False and "SONDER_BLENDER" in payload["error"]
    assert recorded == [("mesh_generate", False)]


def test_the_tool_is_auto_discovered():
    names = [name for name, _ in agent_help_tools.discover_agent_tool_registrars()]
    assert "mesh_agent_tools" in names


def test_blank_brief_is_an_error_payload_not_an_exception():
    mcp = FakeMcp()
    mesh_agent_tools.register(mcp, lambda *a, **k: None)
    assert json.loads(mcp.tools["mesh_generate"](""))["ok"] is False


def test_unknown_tier_is_refused(tmp_path):
    with pytest.raises(mesh_agent_tools.SonderError, match="unknown tier"):
        call(tmp_path, tiers="general,nonsense")


def test_end_to_end_with_fakes_writes_final_files_and_clamps(tmp_path):
    payload, runner, rt = call(tmp_path, name="Hand Cart", candidates=99, rounds=99, max_tris=1,
                               timeout=5, target_score=9)
    assert payload["ok"] is True and payload["name"] == "hand-cart" and payload["vision"] == "on"
    assert len(payload["candidates"]) == 6
    assert all(kw["max_tris"] == 50 for _, _, kw in runner.calls)
    assert rt.generated and all(kw["num_predict"] == 4096 and kw["num_ctx"] == 16384 and kw["temperature"] == 0.3
                                for _, kw in rt.generated)
    assert rt.vision_calls and all(kw["extra_roots"] == payload["output_dir"] for _, kw in rt.vision_calls)
    final = payload["final"]
    assert set(final) == {"model", "sheet", "view", "code"}
    assert final["model"].endswith("hand-cart.glb")
    out = json.dumps(payload)
    assert "def build" not in out
    assert "meshgen" in payload["output_dir"].replace("\\", "/") and "hand-cart-" in payload["output_dir"]


def test_vision_failure_is_caught_once_and_reported(tmp_path):
    rt = fake_runtime(tmp_path, vision_error="vision tier is not set")
    payload, _, _ = call(tmp_path, rt=rt, candidates=2, rounds=2)
    assert payload["ok"] is True
    assert payload["vision"].startswith("unavailable") and "vision tier is not set" in payload["vision"]
    assert len(rt.vision_calls) == 1 or len(rt.vision_calls) == 2  # at most one call per racing candidate


def test_failed_builds_return_an_error_payload(tmp_path):
    payload, _, _ = call(tmp_path, runner=FakeRunner(fail=True), candidates=1, rounds=1)
    assert payload["ok"] is False and "no candidate produced a model" in payload["error"]
    assert payload["candidates"][0]["attempts"][0]["status"] == "build_error"


def test_kit_is_imported_by_name_and_digested(tmp_path):
    kit = tmp_path / "propkit.py"
    kit.write_text('def wheel(r):\n    """Make a wheel."""\n', encoding="utf-8")
    runner = FakeRunner()
    rt = fake_runtime(tmp_path)
    prompts = []
    original = rt._generate_text
    rt._generate_text = lambda prompt, **kw: prompts.append(prompt) or original(prompt, **kw)
    payload, runner, _ = call(tmp_path, runner=runner, rt=rt, kit_path=str(kit), candidates=1, rounds=1)
    assert "import propkit" in prompts[0] and "def wheel(r)" in prompts[0]
    assert runner.calls[0][2]["kit_path"] == str(kit.resolve())


def test_output_dir_outside_the_workspace_is_refused(tmp_path):
    inside = tmp_path / "ws"
    inside.mkdir()
    rt = fake_runtime(inside)
    with pytest.raises(mesh_agent_tools.SonderError, match="inside the asset workspace"):
        call(tmp_path, rt=rt, output_dir=str(tmp_path / "elsewhere"))
