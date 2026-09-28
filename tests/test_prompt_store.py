"""Editable prompts: overrides, fallbacks, per-turn pinning, provenance, /prompts."""
import logging
import os
import pathlib

import pytest

from sonder_runtime.adapters import prompt_store
from sonder_runtime.domain import prompt_templates

REPO_PROMPTS = pathlib.Path(__file__).resolve().parents[1] / "prompts"


@pytest.fixture
def override_dir(tmp_path, monkeypatch):
    directory = tmp_path / "overrides"
    directory.mkdir()
    monkeypatch.setenv("SONDER_PROMPTS_DIR", str(directory))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    prompt_store.reload()
    yield directory
    prompt_store.reload()


def _write(path, data, *, bump=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    if bump:
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + bump))


# --- override wins and is re-read -------------------------------------------

def test_override_wins_and_is_reread_after_edit(override_dir):
    target = override_dir / "trace_instructions.md"
    _write(target, "Think first.\n")
    first = prompt_store.load("trace_instructions")
    assert (first.source, prompt_store.render("trace_instructions")) == ("override", "Think first.")
    assert first.label == "override@" + prompt_templates.digest("Think first.")[:8]

    _write(target, "Think carefully, then answer.\n", bump=10_000_000)
    assert prompt_store.render("trace_instructions") == "Think carefully, then answer."

    target.unlink()
    assert prompt_store.load("trace_instructions").source == "default"


def test_state_home_override_is_used_when_no_env_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("SONDER_PROMPTS_DIR", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    prompt_store.reload()
    _write(tmp_path / "home" / "prompts" / "personas" / "teacher.md", "Teach gently.\n")
    import personas
    assert personas.get("teacher") == "Teach gently."
    assert personas.PERSONAS["teacher"] == "Teach gently."
    assert personas.get("coder") == (REPO_PROMPTS / "personas" / "coder.md").read_text(
        encoding="utf-8").replace("\r\n", "\n").rstrip("\n")


def test_template_override_renders_fields(override_dir):
    _write(override_dir / "execution_router.md", "P=$project R=$request costs $$5\n")
    assert prompt_store.render("execution_router", project="x", request="y") == "P=x R=y costs $5"


def test_crlf_and_bom_are_normalised(override_dir):
    _write(override_dir / "agent_hosted.md", b"\xef\xbb\xbfline one\r\nline two\r\n")
    assert prompt_store.render("agent_hosted") == "line one\nline two"


# --- bad overrides fall back with one warning --------------------------------

@pytest.mark.parametrize("name,data,reason", [
    ("agent_hosted", "   \n\n", "empty"),
    ("agent_hosted", "x" * (prompt_store.MAX_BYTES + 1), "larger than"),
    ("agent_hosted", b"\xff\xfe not utf8 \x80", "not valid UTF-8"),
    ("execution_router", "Project $project only\n", "missing placeholder(s) $request"),
    ("execution_router", "$project $request $surprise\n", "unknown placeholder(s) $surprise"),
    ("execution_router", "$project $request costs $5\n", "malformed placeholder"),
    ("ollama_alias_system", 'You are """ hacked\n', 'contains """'),
], ids=["empty", "oversized", "non-utf8", "missing-field", "unknown-field", "bad-dollar", "modelfile-quote"])
def test_bad_override_falls_back_to_default_with_one_warning(override_dir, caplog, name, data, reason):
    _write(override_dir / (name + ".md"), data)
    fields = {f: "v" for f in prompt_store.CATALOG[name].fields}
    default_text = prompt_templates.render(
        prompt_templates.normalize((REPO_PROMPTS / (name + ".md")).read_text(encoding="utf-8")), fields,
    )
    with caplog.at_level(logging.WARNING, logger="sonder.prompts"):
        assert prompt_store.render(name, **fields) == default_text
        assert prompt_store.render(name, **fields) == default_text
    warnings = [r for r in caplog.records if r.name == "sonder.prompts"]
    assert len(warnings) == 1
    assert reason in warnings[0].getMessage()
    loaded = prompt_store.load(name)
    assert loaded.source == "default" and reason in loaded.note


def test_symlink_escaping_the_override_dir_is_ignored(override_dir, tmp_path, caplog):
    outside = tmp_path / "outside.md"
    outside.write_text("stolen instructions\n", encoding="utf-8")
    link = override_dir / "agent_hosted.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/account")
    with caplog.at_level(logging.WARNING, logger="sonder.prompts"):
        text = prompt_store.render("agent_hosted")
    assert "stolen" not in text
    assert prompt_store.load("agent_hosted").source == "default"
    assert any("outside its override directory" in r.getMessage() for r in caplog.records)


def test_unknown_names_and_traversal_are_refused(override_dir):
    for name in ("../server", "personas/../../x", "nope"):
        with pytest.raises(KeyError):
            prompt_store.load(name)
    assert "unknown prompt" in prompt_store.command("show ../server")


def test_render_rejects_wrong_caller_fields(override_dir):
    with pytest.raises(prompt_templates.PromptTemplateError):
        prompt_store.render("execution_router", project="x")


def test_missing_shipped_default_is_loud_not_silent(override_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(prompt_store, "repo_dir", lambda: tmp_path / "no-prompts")
    with pytest.raises(prompt_store.PromptUnavailable):
        prompt_store.render("agent_hosted")
    assert "MISSING" in prompt_store.command("list")


# --- per-turn pinning and provenance ------------------------------------------

def test_turn_scope_pins_text_and_records_provenance(override_dir):
    target = override_dir / "agent_hosted.md"
    _write(target, "version one\n")
    with prompt_store.turn_scope():
        assert prompt_store.render("agent_hosted") == "version one"
        _write(target, "version two!\n", bump=10_000_000)
        assert prompt_store.render("agent_hosted") == "version one"
        prompt_store.render("trace_instructions")
        with prompt_store.turn_scope():  # re-entrant: keeps the outer pin
            assert prompt_store.render("agent_hosted") == "version one"
        provenance = prompt_store.current_provenance()
    assert provenance == {
        "agent_hosted": "override@" + prompt_templates.digest("version one")[:8],
        "trace_instructions": "default",
    }
    assert prompt_store.current_provenance() == {}
    assert prompt_store.render("agent_hosted") == "version two!"


def test_stable_system_context_pins_prompts(override_dir):
    import server
    target = override_dir / "trace_instructions.md"
    _write(target, "Reason A.\n")
    with server._stable_system_context():
        first = server._build_system("base", True, "")
        _write(target, "Reason B, longer.\n", bump=10_000_000)
        second = server._build_system("base", True, "")
        assert prompt_store.current_provenance()["trace_instructions"].startswith("override@")
    assert first == second and "Reason A." in first
    assert "Reason B, longer." in server._build_system("base", True, "")


def test_turn_trace_records_prompt_provenance(override_dir):
    from sonder_runtime.adapters.observability import trace_buffer
    _write(override_dir / "personas" / "coder.md", "Code well.\n")
    with prompt_store.turn_scope():
        prompt_store.render("personas/coder")
        prompt_store.render("trace_instructions")
        trace_buffer._capture_turn("m", "code", {"augmented_prompt": "p"}, "q", "a")
        formatted = trace_buffer._format_trace("m", "code", {}, {"augmented_prompt": "p"})
    captured = trace_buffer._TURN_TRACES[-1]
    assert captured["prompts"] == {
        "personas/coder": "override@" + prompt_templates.digest("Code well.")[:8],
        "trace_instructions": "default",
    }
    assert "prompts: personas/coder=override@" in formatted


def test_chat_entry_points_are_turn_scoped(override_dir):
    """Both chat entry points pin prompts, so their turn traces carry provenance."""
    import server
    for entry in (server._sonder_impl_serialized, server._answer_with_history_impl):
        assert getattr(entry, "__prompt_turn_scoped__", False) is True, entry
    with prompt_store.turn_scope():
        server._build_system("", False, "teacher", model="m")
        used = prompt_store.current_provenance()
    assert used == {"personas/teacher": "default", "runtime_identity": "default"}


# --- /prompts ----------------------------------------------------------------

def test_prompts_list_show_path_reload(override_dir):
    _write(override_dir / "agent_hosted.md", "custom hosted\n")
    _write(override_dir / "execution_router.md", "no placeholders at all\n")
    listing = prompt_store.command("")
    assert listing == prompt_store.command("list")
    for name in prompt_store.CATALOG:
        assert name in listing
    hosted_line = next(line for line in listing.splitlines() if "agent_hosted" in line)
    assert "override@" + prompt_templates.digest("custom hosted")[:8] in hosted_line
    router_line = next(line for line in listing.splitlines() if " execution_router " in line + " ")
    assert "default" in router_line and "ignored" in router_line

    shown = prompt_store.command("show agent_hosted")
    assert shown.endswith("\n\ncustom hosted")
    assert "$project $request" in prompt_store.command("show execution_router")

    path_text = prompt_store.command("path personas/coder")
    assert str(override_dir / "personas" / "coder.md") in path_text
    assert "placeholders" not in path_text
    assert "$workspace_root $tools" in prompt_store.command("path child_lane")

    assert "cleared" in prompt_store.command("reload")
    assert prompt_store.command("frobnicate") == prompt_store.PROMPTS_USAGE
    assert prompt_store.command("show") == prompt_store.PROMPTS_USAGE


def test_prompts_command_is_reachable_from_the_console_chain(override_dir):
    import server
    out = server.control_command("/prompts list")
    assert "trace_instructions" in out and "override dirs" in out


# --- other consumers ----------------------------------------------------------

def test_child_lane_fallback_matches_the_shipped_default():
    from sonder_runtime.application.agents import interactive_lanes
    shipped = prompt_templates.normalize(
        (REPO_PROMPTS / "child_lane.md").read_text(encoding="utf-8"))
    assert interactive_lanes._CHILD_LANE_FALLBACK == shipped


def test_module_constants_remain_readable(override_dir):
    import grounded_extraction
    import reflection
    import self_curriculum
    _write(override_dir / "reflection_distill_system.md", "Distil.\n")
    assert reflection.DISTILL_SYSTEM == "Distil."
    assert reflection.PITFALL_SYSTEM.startswith("You extract ONE concrete, reusable pitfall")
    assert grounded_extraction.EXTRACTION_SYSTEM.startswith("You extract facts")
    assert self_curriculum.GEN_PROMPT.endswith("before or after.\n")
    with pytest.raises(AttributeError):
        _ = reflection.NOT_A_PROMPT


def test_setup_alias_modelfile_uses_the_editable_prompt(override_dir):
    import setup_alias
    _write(override_dir / "ollama_alias_system.md", "Be Sonder.\n")
    assert setup_alias.model_file("base:7b") == (
        'FROM base:7b\nPARAMETER temperature 0.2\nSYSTEM """Be Sonder."""\n'
    )


def test_every_catalog_prompt_is_shipped_and_packaged():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "package_local_system",
        pathlib.Path(__file__).resolve().parents[1] / "scripts" / "package_local_system.py",
    )
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    for name in prompt_store.CATALOG:
        rel = pathlib.Path("prompts") / (name + ".md")
        assert (REPO_PROMPTS.parent / rel).is_file(), rel
        assert package._included(rel), rel
        assert rel.as_posix() in package.REQUIRED_FILES, rel
