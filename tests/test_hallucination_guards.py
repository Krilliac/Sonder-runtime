"""Comprehensive tests for Sonder's anti-hallucination tools and always-on host guards:
- symbol_verify (/symcheck)
- citation_audit (/citecheck)
- fact_cross_check (/crosscheck)
- consistency_check (/consensus)
- code_gate_policy.check_python_stdlib_api_errors
- honesty.guard_chat_turn_hallucinations (contradicted runs, fake web, fake read)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import server
from sonder_runtime.adapters.inspection import (
    citation_audit as ca_mod,
    consistency_check as cc_mod,
    fact_cross_check as fc_mod,
    symbol_verify as sv_mod,
)
from sonder_runtime.application.chat import honesty
from sonder_runtime.domain.chat_tools import CORE_CHAT_TOOL_DEFINITIONS, ChatToolRecord
from sonder_runtime.domain.code_gate_policy import check_python_static_errors
from sonder_runtime.domain.speculation_policy import is_speculatable
from sonder_runtime.domain.tools.builtin_traits import builtin_traits


# --- 1. symbol_verify ---------------------------------------------------------


def test_symbol_verify_stdlib_valid_invalid_and_kwargs():
    ok = sv_mod.symbol_verify(module="json", symbol="loads", kwargs=["s", "strict"])
    assert ok["ok"] is True and ok["verified"] is True

    bad_attr = sv_mod.symbol_verify(module="json", symbol="parse")
    assert bad_attr["ok"] is True and bad_attr["verified"] is False
    assert "does not exist" in bad_attr["reason"]

    bad_kw = sv_mod.symbol_verify(module="math", symbol="isclose", kwargs=["rel_tol", "epsilon_fake"])
    assert bad_kw["ok"] is True and bad_kw["verified"] is False
    assert "epsilon_fake" in bad_kw["invalid_kwargs"]

    bad_mod = sv_mod.symbol_verify(module="definitely_nonexistent_pkg_xyz_999")
    assert bad_mod["ok"] is True and bad_mod["verified"] is False


def test_symbol_verify_audit_code_and_repo_symbols(tmp_path: Path, monkeypatch):
    proj = tmp_path / "repo"
    proj.mkdir()
    (proj / "mylib.py").write_text(
        "class Engine:\n    def start(self, port: int = 8080):\n        return port\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(proj))

    good_repo = sv_mod.symbol_verify(module="mylib", symbol="Engine.start", kwargs=["port"], root=str(proj))
    assert good_repo["verified"] is True

    bad_repo = sv_mod.symbol_verify(module="mylib", symbol="Engine.launch", root=str(proj))
    assert bad_repo["verified"] is False

    snippet = (
        "import json\n"
        "import imaginary_hallucinated_lib_xyz\n"
        "from math import nonexistent_fn\n"
        "x = json.parse('{}')\n"
    )
    audit = sv_mod.symbol_verify(action="audit_code", code=snippet)
    assert audit["ok"] is True and audit["clean"] is False
    kinds = {i["kind"] for i in audit["issues"]}
    assert "hallucinated_import" in kinds
    assert "hallucinated_symbol" in kinds
    assert "hallucinated_attribute" in kinds


# --- 2. citation_audit --------------------------------------------------------


def test_citation_audit_files_lines_quotes_and_sources(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text(
        "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(proj))

    # Prose file:line audit catches nonexistent file and out-of-range line
    prose = "See app.py:2, app.py:99, and ghost.py:5 for details."
    res = ca_mod.citation_audit(text=prose, root=str(proj))
    assert res["ok"] is True and res["clean"] is False
    by_key = {(r["file"], r["line"]): r["status"] for r in res["all_results"]}
    assert by_key[("app.py", 2)] == "verified"
    assert by_key[("app.py", 99)] == "line_out_of_range"
    assert by_key[("ghost.py", 5)] == "fabricated_file"

    # Explicit quote claims: exact, drifted, fabricated
    qres = ca_mod.citation_audit(
        root=str(proj),
        claims=[
            {"file": "app.py", "line": 2, "quote": "return 1"},
            {"file": "app.py", "line": 1, "quote": "return 2"},
            {"file": "app.py", "line": 2, "quote": "return 999"},
        ],
    )
    statuses = [r["status"] for r in qres["all_results"]]
    assert statuses[0] == "verified"
    assert statuses[2] == "fabricated_quote"

    # Source-grounding check without repo
    sres = ca_mod.citation_audit(
        text='The doc says "total latency was 42 ms" and also "memory dropped to zero bytes".',
        sources=["Benchmark report: total latency was 42 ms under load."],
    )
    assert sres["verified_count"] == 1
    assert sres["issue_count"] == 1
    assert sres["issues"][0]["status"] == "ungrounded_quote"


# --- 3. fact_cross_check ------------------------------------------------------


def test_fact_cross_check_catches_math_tables_dates_counts_and_contradictions():
    draft = (
        "On Monday, 2026-11-22 (and also 2026-02-30), we computed 12 * 14 = 160 and 15% of 240 = 42.\n\n"
        "Here are the following 3 reasons:\n"
        "- First reason\n"
        "- Second reason\n\n"
        "| Item | Cost |\n"
        "| --- | --- |\n"
        "| CPU | 40 |\n"
        "| RAM | 35 |\n"
        "| Total | 90 |\n\n"
        "Strict mode is enabled by default. Later we note strict mode is disabled by default.\n"
    )
    res = fc_mod.fact_cross_check(draft)
    assert res["ok"] is True and res["clean"] is False
    kinds = {i["kind"] for i in res["issues"]}
    assert "wrong_weekday" in kinds
    assert "invalid_calendar_date" in kinds
    assert "arithmetic_error" in kinds
    assert "percentage_error" in kinds
    assert "list_count_mismatch" in kinds
    assert "table_total_mismatch" in kinds
    assert "self_contradiction" in kinds

    # Clean factual prose passes with 0 issues
    clean = (
        "On Sunday, 2026-11-22, 12 * 14 = 168 and 15% of 240 = 36.\n"
        "Here are the following 2 items:\n- A\n- B\n"
    )
    res_clean = fc_mod.fact_cross_check(clean)
    assert res_clean["ok"] is True and res_clean["clean"] is True


# --- 4. consistency_check -----------------------------------------------------


def test_consistency_check_candidates_and_receipt_auditing():
    div = cc_mod.consistency_check(
        action="compare_candidates",
        candidates=[
            "Yes, the limit is 42 in app.py.",
            "No, the limit is 99 in server.py.",
        ],
    )
    assert div["ok"] is True
    assert div["verdict_conflict"] is True
    assert div["hallucination_risk"] == "high"
    assert "42" in div["contested_numbers"] and "99" in div["contested_numbers"]

    agree = cc_mod.consistency_check(
        action="compare_candidates",
        candidates=[
            "Yes, the timeout is 30 seconds in app.py.",
            "Yes, the timeout is 30 seconds in app.py.",
            "Yes, the timeout is 30 seconds in app.py.",
        ],
    )
    assert agree["ok"] is True and agree["verdict_conflict"] is False
    assert "30" in agree["consensus_numbers"]
    assert agree["hallucination_risk"] == "low"

    # Receipt audit catches contradicted pass claim and unverified write
    rec_audit = cc_mod.consistency_check(
        action="audit_against_receipts",
        reply="All tests passed! I also wrote fixed_app.py.",
        receipts=[
            {"tool": "test_run", "status": "failed", "exit_code": 1},
        ],
    )
    assert rec_audit["ok"] is True and rec_audit["clean"] is False
    kinds = {i["kind"] for i in rec_audit["issues"]}
    assert "contradicted_execution_pass" in kinds
    assert "unverified_file_write" in kinds


# --- 5. Always-on host guards (code_gate_policy + honesty) --------------------


def test_always_on_static_stdlib_api_guard_in_code_gate():
    bad_code = "import json\ndef load(s):\n    return json.parse(s)\n"
    err = check_python_static_errors(bad_code)
    assert err is not None and "json" in err and "parse" in err

    bad_kw_code = "import math\ndef near(a, b):\n    return math.isclose(a, b, nope=1)\n"
    err2 = check_python_static_errors(bad_kw_code)
    assert err2 is not None and "nope" in err2

    good_code = "import json, math\ndef f(s):\n    return math.isclose(json.loads(s), 1.0, rel_tol=1e-6)\n"
    assert check_python_static_errors(good_code) is None


def test_always_on_honesty_guards_for_contradicted_runs_web_and_reads():
    # Contradicted execution pass (run_code failed, model claims tests passed)
    failed_rec = ChatToolRecord("c1", "test_run", "failed", "host", exit_code=1)
    out = honesty.guard_chat_turn_hallucinations("All tests passed!", [failed_rec])
    assert "failed" in out and "do not treat pass claims above as verified" in out

    # Passing test_run suppresses the warning
    ok_rec = ChatToolRecord("c2", "test_run", "ok", "host", exit_code=0)
    assert honesty.guard_chat_turn_hallucinations("All tests passed!", [ok_rec]) == "All tests passed!"

    # Fake web search claim when no web tool ran
    web_out = honesty.guard_chat_turn_hallucinations("I searched the web and found version 9.9.", [])
    assert "no web search or fetch tool succeeded" in web_out

    # Negated web claim is not flagged
    neg_web = honesty.guard_chat_turn_hallucinations("I did not search the web for this.", [])
    assert "no web search or fetch tool succeeded" not in neg_web

    # Fake file read claim when no read tool ran
    read_out = honesty.guard_chat_turn_hallucinations("I just read server.py and found a bug.", [])
    assert "no file-reading tool succeeded" in read_out

    # Passing file_read suppresses the warning
    ok_read = ChatToolRecord("c3", "file_read", "ok", "workspace")
    assert honesty.guard_chat_turn_hallucinations("I just read server.py and found a bug.", [ok_read]) == "I just read server.py and found a bug."


# --- 6. Cross-surface registration --------------------------------------------


def test_anti_hallucination_tools_wired_across_surfaces():
    sym_out = json.loads(server._agent_dispatch("symbol_verify", {"module": "json", "symbol": "parse"}))
    assert sym_out["ok"] is True and sym_out["verified"] is False

    fc_out = json.loads(server.control_command("/crosscheck 10 + 10 = 25"))
    assert fc_out["ok"] is True and fc_out["clean"] is False

    cons_out = json.loads(server.control_command("/consensus Yes, 10 || No, 20"))
    assert cons_out["ok"] is True and cons_out["verdict_conflict"] is True

    core_names = {d.name for d in CORE_CHAT_TOOL_DEFINITIONS}
    for name in ("symbol_verify", "citation_audit", "fact_cross_check", "consistency_check"):
        assert name in core_names
        assert builtin_traits(name).is_read_only is True
        assert is_speculatable(name) is True
