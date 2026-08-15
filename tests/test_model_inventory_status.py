"""Regression tests for the operator model inventory in server.status().

The status tool is the surface an operator reaches for when the local model
stack is misbehaving, so it must tolerate a degraded Ollama-compatible
endpoint instead of crashing, must not report a broken catalog as an empty
one, and must actually show the VRAM residency its contract promises.
"""
import pytest

import server


def _fake_get(tags=None, ps=None):
    payloads = {"/api/tags": tags, "/api/ps": ps}

    def fake(path):
        payload = payloads.get(path)
        if payload is None:
            return {"models": []}
        return payload

    return fake


def test_status_tolerates_malformed_rows_and_model_key(monkeypatch):
    """Junk rows are skipped, valid ones survive, `model` key is honoured."""
    monkeypatch.setattr(
        server,
        "_get",
        _fake_get(
            tags={
                "models": [
                    {"name": "alpha:latest"},
                    None,
                    "junk",
                    17,
                    {"model": "beta:latest"},
                    {"name": ""},
                ]
            },
            ps={"models": [None, {"name": "alpha:latest"}]},
        ),
    )

    result = server.status()

    assert "alpha:latest" in result
    assert "beta:latest" in result
    assert "Resident in Ollama now: alpha:latest" in result


@pytest.mark.parametrize(
    "tags",
    [
        ["not", "a", "dict"],
        "maintenance",
        {"models": "garbage"},
        {"models": {"nested": True}},
        {"models": None},
    ],
)
def test_status_reports_invalid_catalog_as_error_not_empty(monkeypatch, tags):
    """A wrong-shape payload is an explicit error, never '(none)' installed."""
    monkeypatch.setattr(server, "_get", _fake_get(tags=tags))

    result = server.status()

    assert "ERROR" in result
    assert "(none)" not in result


def test_status_reports_invalid_residency_as_error_not_empty(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get",
        _fake_get(tags={"models": []}, ps={"models": "garbage"}),
    )

    result = server.status()

    assert "ERROR" in result
    assert "(none loaded)" not in result


def test_status_shows_vram_residency_indicators(monkeypatch):
    """Resident models expose bounded, content-free VRAM indicators."""
    gib = 2**30
    monkeypatch.setattr(
        server,
        "_get",
        _fake_get(
            tags={"models": []},
            ps={
                "models": [
                    {"name": "full:latest", "size": 5 * gib, "size_vram": 5 * gib},
                    {"name": "spill:latest", "size": 4 * gib, "size_vram": 2 * gib},
                    {"name": "cpu:latest", "size": 3 * gib, "size_vram": 0},
                    {"name": "opaque:latest"},
                ]
            },
        ),
    )

    result = server.status()

    assert "full:latest (5.0 GiB, 100% GPU)" in result
    assert "spill:latest (4.0 GiB, 50% GPU)" in result
    assert "cpu:latest (3.0 GiB, CPU only)" in result
    # Missing size metadata degrades to the bare name, never a guess.
    assert "opaque:latest (" not in result
    assert "opaque:latest" in result


def test_status_vram_indicator_ignores_malformed_sizes(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get",
        _fake_get(
            tags={"models": []},
            ps={
                "models": [
                    {"name": "bool:latest", "size": True, "size_vram": True},
                    {"name": "neg:latest", "size": -5, "size_vram": -1},
                    {"name": "text:latest", "size": "big", "size_vram": "most"},
                    # Oversized JSON integers must not crash float validation.
                    {"name": "huge:latest", "size": 10**1000, "size_vram": 10**1000},
                    # size_vram larger than size is provider nonsense; clamp
                    # rather than advertising >100% GPU.
                    {"name": "over:latest", "size": 2**30, "size_vram": 2**31},
                ]
            },
        ),
    )

    result = server.status()

    assert "bool:latest (" not in result
    assert "neg:latest (" not in result
    assert "text:latest (" not in result
    assert "huge:latest (" not in result
    assert "huge:latest" in result
    assert "over:latest (1.0 GiB, 100% GPU)" in result


def test_status_deduplicates_installed_names_casefolded(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get",
        _fake_get(
            tags={
                "models": [
                    {"name": "Alpha:latest"},
                    {"name": "alpha:latest"},
                ]
            }
        ),
    )

    result = server.status()

    installed_line = next(
        line for line in result.splitlines()
        if line.startswith("Installed/registered models:")
    )
    assert installed_line.count("lpha:latest") == 1


def test_diagnostics_ollama_line_tolerates_malformed_rows(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get",
        _fake_get(
            tags={"models": [{"name": "alpha:latest"}, None, {"model": "beta:latest"}]}
        ),
    )

    result = server.diagnostics()

    ollama_line = next(
        line for line in result.splitlines() if line.strip().startswith("ollama:")
    )
    assert "ollama: ok (2 models: alpha:latest, beta:latest)" in ollama_line


def test_diagnostics_ollama_line_names_invalid_payload(monkeypatch):
    monkeypatch.setattr(server, "_get", _fake_get(tags={"models": "garbage"}))

    result = server.diagnostics()

    ollama_line = next(
        line for line in result.splitlines() if line.strip().startswith("ollama:")
    )
    assert "ERROR" in ollama_line
    assert "attribute" not in ollama_line  # not a raw AttributeError string
