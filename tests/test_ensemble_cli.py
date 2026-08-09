from pathlib import Path

import server


def test_ensemble_control_command_requires_a_question():
    assert server.control_command("/ensemble") == (
        "usage: /ensemble <question>   "
        "(polls several local tiers, then compounds one answer)"
    )


def test_ensemble_control_command_forwards_project(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_resolve_project", lambda value: f"resolved:{value}")
    monkeypatch.setattr(
        server,
        "ensemble_answer",
        lambda prompt, **kwargs: calls.append((prompt, kwargs)) or "answer",
    )

    assert server.control_command(
        "/ensemble compare the candidates", project="demo"
    ) == "answer"
    assert calls == [
        ("compare the candidates", {"project": "resolved:demo"})
    ]


def test_repl_routes_ensemble_as_a_control_command():
    source = (Path(__file__).parents[1] / "sonder_repl.py").read_text(
        encoding="utf-8"
    )
    assert '"/goal", "/goals", "/ensemble"' in source
