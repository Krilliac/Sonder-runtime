"""Defects the surface sweep found (scripts/surface_sweep.py), pinned.

The sweep drives every catalogued command on every surface with synthesised
arguments and classifies what comes back. Three things it turned up that no
unit test had asked:

* ``model_fanout`` let a transport error out of the tool body as a raw
  traceback on every surface when no model endpoint was reachable;
* the native MCP inspections crashed with ``KeyError`` when a client sent
  only the arguments its schema requires, because the executor indexed every
  bound the legacy handler fills from its signature;
* the natural-language router resolved "ground artifact" to
  ``/artifact_ground`` -- a different tool whose name is the same two words
  in the other order -- because summary words broke the tie instead of the
  order the name was spoken in.

And one thing the sweep did to itself: its probes appended to the checkout's
own ``system_profile.md`` and rewrote the tracked ``emotion_vectors.json``,
because those modules anchor their file to their own directory and the
harness had redirected only ``file_ops``. The sweep now redirects every such
root and audits the checkout; the tests at the end keep both honest.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

import server
from sonder_runtime.adapters import inspection_executor
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.tool_executor import ToolCall
from sonder_runtime.interfaces.repl import command_router

pytestmark = pytest.mark.unit


# --- model_fanout ----------------------------------------------------------------


def test_a_fanout_transport_failure_is_a_policy_answer_not_a_traceback(monkeypatch):
    def refused(*_args, **_kwargs):
        raise urllib.error.URLError("[Errno 111] Connection refused")

    monkeypatch.setattr(server, "_fanout_start", refused)
    out = server._model_fanout_authorized("sweep probe", num_predict=32, timeout=5)
    assert out.startswith("ERROR"), out
    assert "Connection refused" in out or "transport" in out


# --- native inspections ------------------------------------------------------------


INSPECTIONS = sorted(inspection_executor._INSPECTION_DEFAULTS) + ["archive_list"]


@pytest.mark.parametrize("tool", INSPECTIONS)
def test_inspection_defaults_mirror_the_legacy_tool_signatures(tool):
    signature = inspect.signature(getattr(server, tool))
    for key, value in inspection_executor.inspection_defaults(tool).items():
        assert key in signature.parameters, (tool, key)
        assert signature.parameters[key].default == value, (tool, key)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.txt").write_text("needle\nsecond\n", encoding="utf-8")
    (root / "data.json").write_text('{"a": 1}\n', encoding="utf-8")
    (root / "app.log").write_text("INFO ok\nERROR boom\n", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    return root


@pytest.mark.parametrize("tool, arguments", [
    ("log_inspect", {"path": "app.log"}),
    ("data_inspect", {"path": "data.json"}),
    ("data_query", {"path": "data.json"}),
    ("file_digest", {"path": "notes.txt"}),
    ("directory_digest", {}),
    ("dependency_inventory", {}),
    ("project_detect", {}),
    ("workspace_compare", {"left": "notes.txt", "right": "data.json"}),
    ("archive_list", {"path": "notes.txt"}),
])
def test_an_inspection_with_only_its_required_arguments_never_raises_keyerror(
    workspace, tool, arguments,
):
    context = local_owner_context(correlation_id="sweep", workspace_roots=(workspace,))
    result = inspection_executor.InspectionExecutorAdapter().execute(
        ToolCall(tool, arguments), context,
    )
    assert result.error_code != "KeyError", result.output
    assert "KeyError" not in str(result.output)


# --- the router ---------------------------------------------------------------------


def test_a_name_spoken_in_its_own_order_beats_its_permutation():
    assert command_router.resolve("ground artifact") == "/ground_artifact"
    explained = command_router.explain("ground artifact")
    assert explained["resolved"] == "/ground_artifact"
    # The other order opens with a word that is itself a one-word command
    # (``/artifact`` is an alias of ``/asset``), and the router stays
    # conservative about that: nothing resolves, and the trace says why.
    assert command_router.resolve("artifact ground") is None
    assert command_router.explain("artifact ground")["detail"]["reason"] == "single-word-name-refused"


# --- the native surface -------------------------------------------------------------


def test_native_routes_only_the_supported_inspections_to_the_inspection_service():
    from sonder_runtime.bootstrap import native_mcp

    assert native_mcp._INSPECTION_NAMES == inspection_executor.SUPPORTED_INSPECTIONS
    adapter = inspection_executor.InspectionExecutorAdapter()
    assert set(adapter._handlers()) == inspection_executor.SUPPORTED_INSPECTIONS
    for name in ("web_fetch", "web_search", "weather_lookup", "approximate_location_lookup",
                 "process_list", "process_memory_risk_inspect", "artifact_risk_inspect",
                 "fetch_artifact", "verify_artifact"):
        assert name not in native_mcp._INSPECTION_NAMES, name


def test_native_run_tools_have_bounded_schemas():
    from sonder_runtime.bootstrap.native_mcp import native_tool_registry

    registry = native_tool_registry()
    assert registry.require("run_program").input_schema["required"] == ["program"]
    assert registry.require("run_script").input_schema["required"] == ["path"]
    for name in ("run_program", "run_script"):
        assert registry.require(name).input_schema["additionalProperties"] is False


@pytest.mark.parametrize("tool, arguments", [
    ("json_patch", {"path": "data.json"}),
    ("file_batch_write", {}),
])
def test_a_missing_operations_list_is_named_not_a_type_error(workspace, tool, arguments):
    from sonder_runtime.adapters.tool_executor import ToolExecutorAdapter

    context = local_owner_context(correlation_id="sweep", workspace_roots=(workspace,))
    result = ToolExecutorAdapter().execute(ToolCall(tool, arguments), context)
    assert result.ok is False
    assert result.error_code == "ValueError"
    assert "operations" in str(result.output)


# --- the sweep's own hermeticity ---------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_sweep():
    spec = importlib.util.spec_from_file_location(
        "surface_sweep_under_test", REPO_ROOT / "scripts" / "surface_sweep.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module_local_roots():
    """Every module in the tree that resolves a writable root from its own
    ``__file__``: the set the sweep has to redirect."""
    skip = {".git", ".runtime", "venv", ".venv", ".claude", "node_modules", "tests",
            "__pycache__", "build", "dist"}
    found = set()
    for base, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for name in files:
            if not name.endswith(".py"):
                continue
            full = Path(base) / name
            try:
                source = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "\ndef workspace_root(" not in source:
                continue
            relative = full.relative_to(REPO_ROOT).with_suffix("")
            found.add(".".join(relative.parts))
    return found


def test_the_sweep_redirects_every_module_local_root():
    sweep = _load_sweep()
    redirected = set(sweep.Sweep.MODULE_ROOTS) | {
        # Redirected by name in ``boot``; game_forge delegates to assetgen.
        "sonder_runtime.adapters.filesystem.file_ops", "game_forge",
    }
    assert _module_local_roots() <= redirected, (
        "a module anchors a writable root to its own directory and the sweep "
        "does not redirect it: %s" % sorted(_module_local_roots() - redirected))


def test_the_sweep_reports_every_checkout_change_it_makes(tmp_path):
    sweep = _load_sweep()
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["config", "user.email", "sweep@example.invalid"], check=True)
    subprocess.run(git + ["config", "user.name", "sweep"], check=True)
    (repo / "profile.md").write_text("standing instructions\n", encoding="utf-8")
    (repo / "already.txt").write_text("dirty before\n", encoding="utf-8")
    (repo / ".gitignore").write_text("generated/\n", encoding="utf-8")
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "seed"], check=True)
    (repo / "already.txt").write_text("dirty before the sweep\n", encoding="utf-8")

    before = sweep.checkout_state(str(repo))
    assert sweep.checkout_changes(before, sweep.checkout_state(str(repo))) == []

    (repo / "profile.md").write_text("standing instructions\n\nsweep probe\n", encoding="utf-8")
    (repo / "already.txt").write_text("dirty before, changed again\n", encoding="utf-8")
    (repo / "generated").mkdir()
    (repo / "generated" / "asset.bin").write_bytes(b"\x00")
    (repo / "stray.txt").write_text("untracked\n", encoding="utf-8")

    changed = sweep.checkout_changes(before, sweep.checkout_state(str(repo)))
    assert "profile.md" in changed, changed
    assert "already.txt" in changed, "a file dirty before the sweep changed again"
    assert "stray.txt" in changed, changed
    assert any(entry.startswith("generated") for entry in changed), "ignored output is still output"


def test_an_unreadable_checkout_leaves_the_guard_silent(tmp_path):
    sweep = _load_sweep()
    assert sweep.checkout_state(str(tmp_path / "not-a-repo")) == {}

