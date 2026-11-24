"""Tests for Sonder's intelligence & reasoning tool suite:
math_eval, logic_verify, regex_lab, code_graph, diff_semantic, reasoning_board,
and expanded interactive chat tool belt.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import server
from sonder_runtime.adapters.inspection import (
    code_graph as cg_mod,
    diff_semantic as ds_mod,
    logic_verify as lv_mod,
    math_eval as me_mod,
    reasoning_board as rb_mod,
    regex_lab as rl_mod,
)
from sonder_runtime.domain.chat_tools import CORE_CHAT_TOOL_DEFINITIONS, DEFAULT_CHAT_TOOL_BUDGET
from sonder_runtime.domain.speculation_policy import is_speculatable
from sonder_runtime.domain.tools.builtin_traits import builtin_traits


# --- 1. math_eval -------------------------------------------------------------


def test_math_eval_exact_rational_and_high_precision():
    res = me_mod.math_eval("1/3 + 1/6", exact=True)
    assert res["ok"] is True
    assert res["result"]["exact"] == "1/2"
    assert res["result"]["float"] == pytest.approx(0.5)

    dec = me_mod.math_eval("1 / 7", precision=30)
    assert dec["ok"] is True
    assert str(dec["result"]).startswith("0.142857142857142857142857142857")


def test_math_eval_number_theory_combinatorics_and_stats():
    assert me_mod.math_eval("comb(52, 5)")["result"] == 2_598_960
    assert me_mod.math_eval("is_prime(104729)")["result"] is True
    assert me_mod.math_eval("factorize(360)")["result"] == {"2": 3, "3": 2, "5": 1}
    assert me_mod.math_eval("totient(36)")["result"] == 12
    assert me_mod.math_eval("mod_inv(3, 11)")["result"] == 4

    lr = me_mod.math_eval("linreg([1, 2, 3, 4], [3, 5, 7, 9])")["result"]
    assert lr["slope"] == pytest.approx(2.0)
    assert lr["intercept"] == pytest.approx(1.0)
    assert lr["r_squared"] == pytest.approx(1.0)

    cdf = me_mod.math_eval("normal_cdf(0)")["result"]
    assert cdf == pytest.approx(0.5)


def test_math_eval_solve_linear_quadratic_system_and_transcendental():
    lin = me_mod.math_eval("2*x + 5 = 17", action="solve")
    assert lin["ok"] is True and lin["kind"] == "linear"
    assert lin["solutions"][0]["exact"] == "6"

    quad = me_mod.math_eval("x**2 - 5*x + 6 = 0", action="solve")
    assert quad["ok"] is True and quad["kind"] == "quadratic"
    assert quad["solutions"] == [pytest.approx(2.0), pytest.approx(3.0)]

    sys_res = me_mod.math_eval(
        action="solve",
        equations=["2*x + y = 7", "x - y = 2"],
        variable="x,y",
    )
    assert sys_res["ok"] is True
    assert sys_res["solution"]["x"]["exact"] == "3"
    assert sys_res["solution"]["y"]["exact"] == "1"

    num = me_mod.math_eval("cos(x) - x = 0", action="solve", bracket=[0.0, 1.0])
    assert num["ok"] is True
    assert num["solutions"][0] == pytest.approx(0.7390851332, abs=1e-6)


def test_math_eval_matrix_calculus_units_dates_and_safety_bounds():
    det = me_mod.math_eval(action="matrix", matrix_op="det", matrix_a=[[1, 2], [3, 4]])
    assert det["ok"] is True and det["result"]["exact"] == "-2"

    inv = me_mod.math_eval(action="matrix", matrix_op="inv", matrix_a=[[1, 2], [3, 4]])
    assert inv["ok"] is True
    prod = me_mod.math_eval(
        action="matrix",
        matrix_op="matmul",
        matrix_a=[[1, 2], [3, 4]],
        matrix_b=[[-2, 1], [1.5, -0.5]],
    )
    assert prod["result"][0][0]["exact"] == "1"
    assert prod["result"][1][1]["exact"] == "1"

    deriv = me_mod.math_eval("x**3", action="derivative", at=2.0)
    assert deriv["first"] == pytest.approx(12.0, rel=1e-4)

    integ = me_mod.math_eval("sin(x)", action="integrate", low=0.0, high=math.pi)
    assert integ["result"] == pytest.approx(2.0, abs=1e-6)

    conv = me_mod.math_eval(action="convert", value=100.0, from_unit="C", to_unit="F")
    assert conv["result"] == pytest.approx(212.0)
    data_c = me_mod.math_eval(action="convert", value=1.0, from_unit="GiB", to_unit="MiB")
    assert data_c["result"] == pytest.approx(1024.0)

    dt = me_mod.math_eval(action="date", date_a="2026-01-01", date_b="2026-01-31")
    assert dt["days"] == 30

    # Security bounds: reject attribute access, __import__, huge exponents
    assert me_mod.math_eval("__import__('os').system('echo hi')")["ok"] is False
    assert me_mod.math_eval("(1).__class__")["ok"] is False
    assert me_mod.math_eval("2 ** 1000000")["ok"] is False


# --- 2. logic_verify ----------------------------------------------------------


def test_logic_verify_sat_equivalence_implication_cnf_and_csp():
    # De Morgan equivalence proof
    eq = lv_mod.logic_verify("!(A & B)", action="equivalent", formula_b="(!A) | (!B)")
    assert eq["ok"] is True and eq["equivalent"] is True and eq["counterexample"] is None

    # Non-equivalence returns exact counterexample
    neq = lv_mod.logic_verify("A -> B", action="equivalent", formula_b="B -> A")
    assert neq["ok"] is True and neq["equivalent"] is False
    ce = neq["counterexample"]
    assert ce is not None and neq["formula_a_value"] != neq["formula_b_value"]

    # Modus ponens implication proof
    imp = lv_mod.logic_verify(action="implies", premises=["A -> B", "B -> C", "A"], conclusion="C")
    assert imp["ok"] is True and imp["valid"] is True

    # Invalid implication returns counterexample
    bad_imp = lv_mod.logic_verify(action="implies", premises=["A | B", "A -> C"], conclusion="C")
    assert bad_imp["ok"] is True and bad_imp["valid"] is False
    assert bad_imp["counterexample"]["C"] is False

    # Truth table & CNF
    tt = lv_mod.logic_verify("A ^ B", action="truth_table")
    assert tt["ok"] is True and tt["row_count"] == 4 and tt["true_count"] == 2

    cnf = lv_mod.logic_verify("A -> (B & C)", action="cnf")
    assert cnf["ok"] is True and cnf["clause_count"] == 2

    # Finite-domain CSP (graph coloring / scheduling with ordering + sum)
    csp = lv_mod.logic_verify(
        action="csp",
        domains={"A": [1, 2, 3], "B": [1, 2, 3], "C": [1, 2, 3]},
        constraints=[
            {"op": "alldiff", "vars": ["A", "B", "C"]},
            {"op": "<", "vars": ["A", "B"]},
            {"op": "<", "vars": ["B", "C"]},
        ],
    )
    assert csp["ok"] is True and csp["satisfiable"] is True
    assert csp["solution"] == {"A": 1, "B": 2, "C": 3}


# --- 3. regex_lab -------------------------------------------------------------


def test_regex_lab_groups_assertions_substitution_and_redos_detection():
    res = rl_mod.regex_lab(
        r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})",
        text="Release on 2026-11-22 and patch on 2026-12-01",
        mode="findall",
        should_match=["2026-01-01"],
        should_not_match=["26-1-1", "not-a-date"],
        replacement=r"\g<day>/\g<month>/\g<year>",
    )
    assert res["ok"] is True
    assert res["assertions_passed"] is True
    assert res["text_result"]["match_count"] == 2
    assert res["text_result"]["matches"][0]["named_groups"] == {"year": "2026", "month": "11", "day": "22"}
    assert "22/11/2026" in res["text_result"]["substituted"]
    assert res["redos_hazards"] == []

    evil = rl_mod.regex_lab(r"(a+)+$", text="a" * 1000 + "!")
    assert evil["ok"] is True
    assert any(h["kind"] == "nested_quantifier" and h["severity"] == "high" for h in evil["redos_hazards"])
    assert evil["text_result"]["truncated"] is True


# --- 4. code_graph ------------------------------------------------------------


def test_code_graph_cycles_impact_and_call_graph(tmp_path: Path, monkeypatch):
    proj = tmp_path / "repo"
    (proj / "pkg").mkdir(parents=True)
    (proj / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (proj / "pkg" / "core.py").write_text(
        "class Base:\n    pass\n\ndef compute(x):\n    return x + 1\n",
        encoding="utf-8",
    )
    (proj / "pkg" / "service.py").write_text(
        "from pkg.core import Base, compute\n\nclass Impl(Base):\n    def run(self):\n        return compute(41)\n",
        encoding="utf-8",
    )
    (proj / "pkg" / "a.py").write_text("import pkg.b\n", encoding="utf-8")
    (proj / "pkg" / "b.py").write_text("import pkg.a\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(proj))

    summary = cg_mod.code_graph(str(proj), action="summary")
    assert summary["ok"] is True
    assert summary["files_scanned"] == 5
    assert summary["cycle_count"] == 1
    assert summary["cycles"][0] == ["pkg/a.py", "pkg/b.py"]

    impact = cg_mod.code_graph(str(proj), action="impact", target="pkg/core.py")
    assert impact["ok"] is True
    assert any(item["file"] == "pkg/service.py" and item["hops"] == 1 for item in impact["impacted"])

    calls = cg_mod.code_graph(str(proj), action="calls", target="compute")
    assert calls["ok"] is True
    assert any(c["caller"] == "Impl.run" for c in calls["callers"])

    bases = cg_mod.code_graph(str(proj), action="calls", target="Base")
    assert any(s["subclass"] == "Impl" for s in bases["subclasses"])


# --- 5. diff_semantic ---------------------------------------------------------


def test_diff_semantic_python_ast_and_json_structural():
    # Formatting-only Python edit reports semantically_identical=True
    fmt = ds_mod.diff_semantic(
        text_a="def add(a, b):\n    return a + b\n",
        text_b="# comment\ndef add(a,   b):\n    return (a + b)\n",
        kind="python",
    )
    assert fmt["ok"] is True
    assert fmt["semantically_identical"] is True
    assert fmt["formatting_only"] is True

    # Real signature + body change detected cleanly
    sig = ds_mod.diff_semantic(
        text_a="def add(a: int, b: int) -> int:\n    return a + b\n",
        text_b="def add(a: int, b: int, c: int = 0) -> int:\n    return a + b + c\ndef mul(x, y):\n    return x * y\n",
        kind="python",
    )
    assert sig["ok"] is True
    assert sig["semantically_identical"] is False
    assert [s["symbol"] for s in sig["symbols_added"]] == ["mul"]
    assert sig["symbols_modified"][0]["symbol"] == "add"
    assert "signature" in sig["symbols_modified"][0]["changes"]

    # JSON structural diff ignores key ordering/formatting
    jdiff = ds_mod.diff_semantic(
        text_a='{"a": 1, "b": {"x": true, "y": 2}}',
        text_b='{"b": {"y": 3, "x": true, "z": "new"}, "a": 1}',
        kind="json",
    )
    assert jdiff["ok"] is True
    assert jdiff["change_count"] == 2
    by_path = {c["path"]: c["change"] for c in jdiff["changes"]}
    assert by_path == {"b.y": "changed", "b.z": "added"}


# --- 6. reasoning_board -------------------------------------------------------


def test_reasoning_board_facts_hypotheses_audit_and_owner_isolation():
    rb_mod._reset_for_tests()
    assert rb_mod.reasoning_board("init", board_id="bug1", owner="alice", goal="Find auth leak")["ok"] is True
    f1 = rb_mod.reasoning_board("add_fact", board_id="bug1", owner="alice", text="Token sk-1234567890abcdef1234567890 leaked", source="serve.py:5029")
    assert f1["ok"] is True
    assert "sk-1234567890abcdef1234567890" not in f1["fact"]["text"]

    # Unsupported confirmed hypothesis triggers audit finding
    h1 = rb_mod.reasoning_board("add_hypothesis", board_id="bug1", owner="alice", text="Keep-alive leaked role", status="confirmed", confidence=0.95)
    assert h1["ok"] is True
    audit1 = rb_mod.reasoning_board("audit", board_id="bug1", owner="alice")
    assert audit1["clean"] is False
    assert any(i["kind"] == "unsupported_hypothesis" for i in audit1["issues"])

    # Attach grounded evidence -> audit turns clean
    rb_mod.reasoning_board(
        "update_hypothesis",
        board_id="bug1",
        owner="alice",
        hypothesis_id="H1",
        evidence_for="Unit test reproduced unbound role after ExitStack",
        fact_refs=["F1"],
    )
    audit2 = rb_mod.reasoning_board("audit", board_id="bug1", owner="alice")
    assert audit2["clean"] is True

    # Owner isolation: bob cannot see alice's board
    bob_summary = rb_mod.reasoning_board("summary", board_id="bug1", owner="bob")
    assert bob_summary["facts"] == []
    assert bob_summary["hypotheses"] == []


# --- 7. Cross-surface wiring (server MCP, _agent_dispatch, slash, chat belt) --


def test_intelligence_tools_wired_across_surfaces(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "m.py").write_text("def hi():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(proj))

    # Direct MCP + _agent_dispatch + slash commands
    calc_out = json.loads(server._agent_dispatch("math_eval", {"expression": "factorial(6)"}))
    assert calc_out["ok"] is True and calc_out["result"] == 720

    slash_calc = json.loads(server.control_command("/calc 2**10"))
    assert slash_calc["result"] == 1024

    logic_out = json.loads(server._agent_dispatch("logic_verify", {"formula": "A & !A"}))
    assert logic_out["satisfiable"] is False and logic_out["contradiction"] is True

    regex_out = json.loads(server._agent_dispatch("regex_lab", {"pattern": r"\d+", "text": "x=42"}))
    assert regex_out["text_result"]["matched"] is True

    graph_out = json.loads(server._agent_dispatch("code_graph", {"root": str(proj)}))
    assert graph_out["ok"] is True and graph_out["files_scanned"] == 1

    diff_out = json.loads(server._agent_dispatch("diff_semantic", {"text_a": "x = 1", "text_b": "x = 1\n", "kind": "python"}))
    assert diff_out["semantically_identical"] is True

    rb_out = json.loads(server._agent_dispatch("reasoning_board", {"action": "init", "board_id": "t1", "goal": "check"}))
    assert rb_out["ok"] is True

    # Traits, speculation policy, and expanded CORE_CHAT_TOOL_DEFINITIONS
    core_names = {d.name for d in CORE_CHAT_TOOL_DEFINITIONS}
    for name in ("math_eval", "logic_verify", "regex_lab", "code_graph", "diff_semantic", "reasoning_board", "memory_search", "repository_symbol_index", "code_metrics", "claim_check", "schema_validate"):
        assert name in core_names
    for pure_name in ("math_eval", "logic_verify", "regex_lab", "code_graph", "diff_semantic"):
        assert builtin_traits(pure_name).is_read_only is True
        assert is_speculatable(pure_name) is True
    assert is_speculatable("reasoning_board") is False
    assert DEFAULT_CHAT_TOOL_BUDGET.max_model_calls >= 10
