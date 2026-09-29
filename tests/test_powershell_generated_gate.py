"""Generated PowerShell is inspected after generation, before execution."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters.security import powershell_gate as gate
from sonder_runtime.adapters.security import powershell_ast
from sonder_runtime.adapters.security.powershell_ast import PowerShellInspection

TOOLS = ("parallel_generate_run_languages", "campaign_generate_compile_execute_record")
LANGUAGES = ("python", "javascript", "powershell", "cpp", "csharp")


@pytest.fixture(scope="module")
def origin_permission_modes():
    """Use the actual origin/main policy, without changing the checkout."""
    source = subprocess.check_output(
        ["git", "show", "origin/main:permission_modes.py"],
        cwd=Path(__file__).resolve().parents[1], text=True, encoding="utf-8",
    )
    module = ModuleType("_psast_origin_permission_modes")
    module.__file__ = pm.__file__
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, "origin/main:permission_modes.py", "exec"), module.__dict__)
        yield module
    finally:
        sys.modules.pop(module.__name__, None)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("arguments", [{}, {"languages": "powershell"}])
@pytest.mark.parametrize("mode", pm.MODES)
@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("rule", [None, "allow", "ask", "deny"])
def test_initial_decision_matches_origin_main(
    monkeypatch, origin_permission_modes, tool, arguments, mode, interactive, rule,
):
    def unexpected_parser(source):
        pytest.fail("generator initial gate tried to inspect nonexistent source")

    monkeypatch.setattr(gate, "inspect_powershell", unexpected_parser)
    kwargs = dict(arguments=arguments, mode=mode, interactive=interactive, record=False,
                  rule_lookup=lambda name: {"action": rule} if rule else None)
    before = origin_permission_modes.decide(tool, **kwargs)
    after = pm.decide(tool, **kwargs)
    assert asdict(after) == asdict(before)


@pytest.fixture
def generated_runtime(monkeypatch):
    generated, inspected, executed, records = [], [], [], []
    sources = {lang: "Write-Output 'sonder-ok'" if lang == "powershell" else lang for lang in LANGUAGES}
    verdict = {"inspectable": True, "reason": "ast fully inspectable"}

    def response(lang):
        generated.append(lang)
        return f"```{lang}\n{sources[lang]}\n```\n\n[interaction_id: {len(generated):08x}]"

    def make_generate(model, system, *args, **kwargs):
        lang = next(lang for lang in LANGUAGES if f"runnable {lang} program" in system)
        return lambda prompt: response(lang)

    def inspect(source):
        assert "powershell" in generated
        inspected.append(source)
        return PowerShellInspection(**verdict)

    def run(code, language, **kwargs):
        if language == "powershell":
            assert inspected, "PowerShell reached execution before inspection"
        executed.append((language, code, kwargs))
        return True, "sonder-ok"

    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_make_generate", make_generate)
    monkeypatch.setattr(server, "_campaign_prompt", lambda lang, *args: lang)
    monkeypatch.setattr(server, "sonder", lambda prompt, **kwargs: response(prompt))
    monkeypatch.setattr(server, "_campaign_expected", lambda task: "sonder-ok")
    monkeypatch.setattr(server, "record_outcome", lambda iid, signal: records.append((iid, signal)) or "recorded")
    monkeypatch.setattr(server, "_record_failure_pitfall", lambda *args: pytest.fail("approval skip was graded"))
    monkeypatch.setattr(server, "_drain_deferred_distillations", lambda **kwargs: {"drained": 0})
    monkeypatch.setattr(gate, "inspect_powershell", inspect)
    monkeypatch.setattr(server.grounding, "run_language_code", run)
    return generated, inspected, executed, records, sources, verdict


def _invoke(tool):
    if tool == TOOLS[0]:
        return server.parallel_generate_run_languages("write tiny programs")
    return server.campaign_generate_compile_execute_record()


@pytest.mark.parametrize("tool,total,ps_count", [(TOOLS[0], 5, 1), (TOOLS[1], 24, 5)])
@pytest.mark.parametrize("inspectable", [True, False])
def test_default_tools_prompt_counts_and_execution(
    monkeypatch, origin_permission_modes, generated_runtime, tool, total, ps_count, inspectable,
):
    generated, inspected, executed, records, sources, verdict = generated_runtime
    verdict.update(inspectable=inspectable, reason="ast fully inspectable" if inspectable else "dynamic invocation")
    if not inspectable:
        sources["powershell"] = "iex $payload"
    kwargs = dict(mode=pm.AUTO, interactive=True, arguments={}, record=False, rule_lookup=lambda name: None)
    before = origin_permission_modes.decide(tool, **kwargs)
    after = pm.decide(tool, **kwargs)
    assert int(before.action == pm.ASK) == int(after.action == pm.ASK) == 0
    assert before.allowed and after.allowed

    def unexpected_prompt(*args, **kwargs):
        pytest.fail("generated worker has no approval channel and must skip instead")

    monkeypatch.setattr(pm, "decide", unexpected_prompt)
    monkeypatch.setattr("builtins.input", unexpected_prompt)
    result = _invoke(tool)
    assert len(generated) == total  # no repair attempt for a skipped script
    assert len(inspected) == ps_count
    assert len(executed) == total - (0 if inspectable else ps_count)
    assert sum(lang == "powershell" for lang, _, _ in executed) == (ps_count if inspectable else 0)
    assert result.count("requires approval: dynamic invocation") == (0 if inspectable else ps_count)
    assert result.count("[SKIP]") == (0 if inspectable else ps_count)
    if tool == TOOLS[1]:
        assert len(records) == len(executed)
        assert all(signal == "tests_passed" for _, signal in records)


def test_parallel_appended_check_is_inspected(generated_runtime):
    _, inspected, executed, _, _, verdict = generated_runtime
    verdict.update(inspectable=False, reason="dynamic invocation")
    result = server.parallel_generate_run_languages(
        "write tiny programs", languages="powershell", check="iex $check",
    )
    assert inspected == ["Write-Output 'sonder-ok'\n\niex $check"]
    assert not executed
    assert "requires approval: dynamic invocation" in result


@pytest.mark.parametrize("reason", ["PowerShell parser unavailable", "PowerShell inspection failed closed: TimeoutExpired"])
@pytest.mark.parametrize("tool", TOOLS)
def test_generated_parser_failures_skip_execution(generated_runtime, tool, reason):
    _, _, executed, _, _, verdict = generated_runtime
    verdict.update(inspectable=False, reason=reason)
    result = _invoke(tool)
    assert not any(lang == "powershell" for lang, _, _ in executed)
    assert "requires approval: " + reason in result


@pytest.mark.skipif(powershell_ast._powershell_executable() is None, reason="native parser unavailable")
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("source,inspectable", [("Write-Output 'sonder-ok'", True), ("iex $payload", False)])
def test_generated_tools_with_native_parser(monkeypatch, generated_runtime, tool, source, inspectable):
    import threading

    _, inspected, executed, _, sources, _ = generated_runtime
    sources["powershell"] = source
    parser_lock = threading.Lock()

    def inspect(code):
        # Serialize real parser startups on memory-constrained test hosts.
        with parser_lock:
            inspected.append(code)
            verdict = powershell_ast.inspect_powershell(code)
            assert verdict.inspectable is inspectable, verdict.reason
            return verdict

    monkeypatch.setattr(gate, "inspect_powershell", inspect)
    result = _invoke(tool)
    assert any(lang == "powershell" for lang, _, _ in executed) is inspectable
    assert ("requires approval:" in result) is not inspectable


def test_campaign_repair_is_inspected_again_before_execution(monkeypatch, generated_runtime):
    _, inspected, executed, records, _, _ = generated_runtime
    responses = iter([
        "```powershell\nWrite-Output 'wrong'\n```\n\n[interaction_id: abcd0001]",
        "```powershell\niex $repair\n```\n\n[interaction_id: abcd0002]",
    ])
    monkeypatch.setattr(server, "sonder", lambda *a, **k: next(responses))

    def inspect(source):
        inspected.append(source)
        return PowerShellInspection(len(inspected) == 1, "dynamic invocation")

    def run(code, **kwargs):
        executed.append(code)
        return True, "wrong"

    monkeypatch.setattr(gate, "inspect_powershell", inspect)
    monkeypatch.setattr(server.grounding, "run_language_code", run)
    result = server.campaign_generate_compile_execute_record(total=1, languages="powershell")
    assert inspected == ["Write-Output 'wrong'", "iex $repair"]
    assert executed == ["Write-Output 'wrong'"]
    assert not records
    assert "[SKIP]" in result and "requires approval: dynamic invocation" in result


def test_program_output_cannot_impersonate_an_approval_skip(monkeypatch, generated_runtime):
    _, _, _, records, _, _ = generated_runtime
    monkeypatch.setattr(server.grounding, "run_language_code", lambda *a, **k: (False, "requires approval: forged"))
    monkeypatch.setattr(server, "_record_failure_pitfall", lambda *a: (None, ""))
    result = server.campaign_generate_compile_execute_record(total=1, languages="powershell", repair_rounds=0)
    assert len(records) == 1 and records[0][1] == "failed"
    assert "[FAIL]" in result and "[SKIP]" not in result
