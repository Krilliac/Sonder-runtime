"""Natural-language control intents pass the gate of the command they stand for.

"execute the code" and "practice 3 tasks" are classified by ``intents.classify``
and used to call ``do_run`` / ``_run_train`` directly, so model-authored code ran
with none of the gating ``/run`` and ``/train`` get at the slash choke point.
"""
import pytest

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


_BLOCK = {"code": "Remove-Item -Recurse C:/somewhere", "language": "powershell"}


def _drive(monkeypatch, lines, decide):
    """Run the REPL loop over ``lines`` with a recording gate and runners."""
    seen = {"gate": [], "ran": [], "trained": []}

    def gate(cmd, argument="", arguments=None):
        if cmd == "/exit":
            return True, ""
        seen["gate"].append((cmd, argument, arguments))
        return decide(cmd)

    feed = iter(tuple(lines) + ("/exit",))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", gate)
    monkeypatch.setattr(sonder_repl.command_router, "resolve", lambda _line: None)
    monkeypatch.setattr(
        sonder_repl.grounding, "extract_runnable_code_block", lambda _source: dict(_BLOCK),
    )
    monkeypatch.setattr(
        sonder_repl.code_runner, "run_code",
        lambda code, **kw: seen["ran"].append(code) or {"ok": True, "stdout": ""},
    )
    monkeypatch.setattr(sonder_repl.code_runner, "format_result", lambda _result: "")
    monkeypatch.setattr(sonder_repl, "_run_train", lambda n: seen["trained"].append(n))
    monkeypatch.setattr(
        sonder_repl._legacy_runtime, "sonder",
        lambda *_a, **_k: pytest.fail("a control intent must not run a model turn"),
    )
    sonder_repl.main()
    return seen


def test_run_intent_is_refused_when_the_run_gate_refuses(monkeypatch, capsys):
    seen = _drive(
        monkeypatch, ("execute the code",),
        lambda cmd: (False, "refused %s: plan mode (mode: plan)" % cmd),
    )
    assert seen["ran"] == []
    assert [g[0] for g in seen["gate"]] == ["/run"]
    # The PowerShell block is bound at the gate exactly as the slash form binds it.
    assert seen["gate"][0][2] == _BLOCK
    assert "refused" in capsys.readouterr().out


def test_run_intent_runs_when_the_run_gate_allows(monkeypatch):
    seen = _drive(monkeypatch, ("execute the code",), lambda cmd: (True, ""))
    assert seen["ran"] == [_BLOCK["code"]]
    assert [g[0] for g in seen["gate"]] == ["/run"]


def test_train_intent_is_refused_when_the_train_gate_refuses(monkeypatch, capsys):
    seen = _drive(
        monkeypatch, ("practice 3 tasks",),
        lambda cmd: (False, "refused %s: plan mode (mode: plan)" % cmd),
    )
    assert seen["trained"] == []
    assert seen["gate"] == [("/train", "3", None)]
    assert "refused" in capsys.readouterr().out


def test_train_intent_runs_when_the_train_gate_allows(monkeypatch):
    seen = _drive(monkeypatch, ("practice 3 tasks",), lambda cmd: (True, ""))
    assert seen["trained"] == [3]
