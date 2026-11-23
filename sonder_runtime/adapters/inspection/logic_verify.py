"""Pure, bounded, stdlib-only propositional SAT, equivalence, implication, and CSP verifier.

Gives Sonder formal Boolean and constraint-verification powers in milliseconds
with zero subprocesses and zero external dependencies:
- ``satisfiable``: DPLL SAT solver returning a concrete witness assignment or UNSAT.
- ``truth_table``: complete truth table (bounded to <= 10 variables).
- ``equivalent``: proves two Boolean formulas logically equivalent OR returns a
  concrete counterexample assignment where they differ.
- ``implies``: proves whether ``premises => conclusion`` is a tautology OR returns
  a concrete counterexample where all premises hold and the conclusion is false.
- ``cnf``: converts a formula into simplified Conjunctive Normal Form clauses.
- ``csp``: finite-domain Constraint Satisfaction solver (backtracking + MRV +
  forward checking) supporting ``alldiff``, ``==``, ``!=``, ``<``, ``<=``, ``>``,
  ``>=``, and linear sum constraints.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

MAX_EXPR_CHARS = 4_000
MAX_VARS = 24
MAX_TRUTH_TABLE_VARS = 10
MAX_DPLL_STEPS = 50_000
MAX_CSP_VARS = 24
MAX_CSP_DOMAIN = 64
MAX_CSP_STEPS = 50_000


class LogicVerifyError(ValueError):
    """Raised on formula syntax or safety-bound errors."""


# --- Recursive-descent propositional logic parser ----------------------------

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<IFF><->|<=>|iff\b)"
    r"|(?P<IMP>->|=>|implies\b)"
    r"|(?P<XOR>\^|xor\b)"
    r"|(?P<AND>&&|&|and\b)"
    r"|(?P<OR>\|\||\||or\b)"
    r"|(?P<NOT>!|~|not\b)"
    r"|(?P<LPAREN>\()"
    r"|(?P<RPAREN>\))"
    r"|(?P<TRUE>\btrue\b|\bTrue\b|1)"
    r"|(?P<FALSE>\bfalse\b|\bFalse\b|0)"
    r"|(?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Node:
    kind: str  # const, var, not, and, or, xor, imp, iff
    value: Any = None
    left: _Node | None = None
    right: _Node | None = None


def _tokenize(text: str) -> list[tuple[str, str]]:
    raw = str(text or "").strip()
    if not raw:
        raise LogicVerifyError("formula is empty")
    if len(raw) > MAX_EXPR_CHARS:
        raise LogicVerifyError(f"formula exceeds {MAX_EXPR_CHARS} characters")
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(raw):
        if raw[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(raw, pos)
        if not m or m.end() == pos:
            raise LogicVerifyError(f"unexpected token near {raw[pos:pos + 20]!r}")
        kind = m.lastgroup or ""
        tok = m.group(kind)
        if kind == "IDENT":
            low = tok.lower()
            if low == "and":
                kind = "AND"
            elif low == "or":
                kind = "OR"
            elif low == "not":
                kind = "NOT"
            elif low == "xor":
                kind = "XOR"
            elif low == "implies":
                kind = "IMP"
            elif low == "iff":
                kind = "IFF"
            elif low == "true":
                kind = "TRUE"
            elif low == "false":
                kind = "FALSE"
        tokens.append((kind, tok))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def consume(self, expected: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise LogicVerifyError("unexpected end of formula")
        if expected is not None and tok[0] != expected:
            raise LogicVerifyError(f"expected {expected}, got {tok[1]!r}")
        self.pos += 1
        return tok

    def parse(self) -> _Node:
        node = self._parse_iff()
        if self.peek() is not None:
            raise LogicVerifyError(f"unexpected trailing token {self.peek()[1]!r}")
        return node

    def _parse_iff(self) -> _Node:
        left = self._parse_imp()
        while self.peek() and self.peek()[0] == "IFF":
            self.consume("IFF")
            right = self._parse_imp()
            left = _Node("iff", left=left, right=right)
        return left

    def _parse_imp(self) -> _Node:
        left = self._parse_or()
        if self.peek() and self.peek()[0] == "IMP":
            self.consume("IMP")
            # Right-associative implication: A -> B -> C == A -> (B -> C)
            right = self._parse_imp()
            return _Node("imp", left=left, right=right)
        return left

    def _parse_or(self) -> _Node:
        left = self._parse_xor()
        while self.peek() and self.peek()[0] == "OR":
            self.consume("OR")
            right = self._parse_xor()
            left = _Node("or", left=left, right=right)
        return left

    def _parse_xor(self) -> _Node:
        left = self._parse_and()
        while self.peek() and self.peek()[0] == "XOR":
            self.consume("XOR")
            right = self._parse_and()
            left = _Node("xor", left=left, right=right)
        return left

    def _parse_and(self) -> _Node:
        left = self._parse_not()
        while self.peek() and self.peek()[0] == "AND":
            self.consume("AND")
            right = self._parse_not()
            left = _Node("and", left=left, right=right)
        return left

    def _parse_not(self) -> _Node:
        if self.peek() and self.peek()[0] == "NOT":
            self.consume("NOT")
            return _Node("not", left=self._parse_not())
        return self._parse_atom()

    def _parse_atom(self) -> _Node:
        tok = self.peek()
        if tok is None:
            raise LogicVerifyError("expected variable, constant, or '('")
        if tok[0] == "LPAREN":
            self.consume("LPAREN")
            inner = self._parse_iff()
            self.consume("RPAREN")
            return inner
        if tok[0] == "TRUE":
            self.consume("TRUE")
            return _Node("const", value=True)
        if tok[0] == "FALSE":
            self.consume("FALSE")
            return _Node("const", value=False)
        if tok[0] == "IDENT":
            self.consume("IDENT")
            return _Node("var", value=tok[1])
        raise LogicVerifyError(f"unexpected token {tok[1]!r}")


def _parse_formula(text: str) -> _Node:
    return _Parser(_tokenize(text)).parse()


def _collect_vars(node: _Node, out: set[str]) -> None:
    if node.kind == "var":
        out.add(str(node.value))
    if node.left is not None:
        _collect_vars(node.left, out)
    if node.right is not None:
        _collect_vars(node.right, out)


def _eval_node(node: _Node, assign: dict[str, bool]) -> bool:
    k = node.kind
    if k == "const":
        return bool(node.value)
    if k == "var":
        return bool(assign[str(node.value)])
    if k == "not":
        return not _eval_node(node.left, assign)  # type: ignore[arg-type]
    lv = _eval_node(node.left, assign)  # type: ignore[arg-type]
    rv = _eval_node(node.right, assign)  # type: ignore[arg-type]
    if k == "and":
        return lv and rv
    if k == "or":
        return lv or rv
    if k == "xor":
        return lv != rv
    if k == "imp":
        return (not lv) or rv
    if k == "iff":
        return lv == rv
    raise LogicVerifyError(f"unknown AST node {k}")


# --- Tseitin CNF conversion + DPLL SAT solver --------------------------------

def _tseitin_cnf(root: _Node, var_names: list[str]) -> tuple[list[list[int]], dict[str, int]]:
    var_to_id: dict[str, int] = {name: idx + 1 for idx, name in enumerate(var_names)}
    next_id = len(var_names) + 1
    clauses: list[list[int]] = []

    def new_var() -> int:
        nonlocal next_id
        vid = next_id
        next_id += 1
        return vid

    def encode(node: _Node) -> int:
        k = node.kind
        if k == "var":
            return var_to_id[str(node.value)]
        if k == "const":
            v = new_var()
            clauses.append([v] if node.value else [-v])
            return v
        if k == "not":
            a = encode(node.left)  # type: ignore[arg-type]
            return -a
        a = encode(node.left)  # type: ignore[arg-type]
        b = encode(node.right)  # type: ignore[arg-type]
        v = new_var()
        if k == "and":
            clauses.extend([[-v, a], [-v, b], [v, -a, -b]])
        elif k == "or":
            clauses.extend([[v, -a], [v, -b], [-v, a, b]])
        elif k == "imp":
            # v <-> (~a | b)
            clauses.extend([[v, a], [v, -b], [-v, -a, b]])
        elif k == "iff":
            clauses.extend([[-v, -a, b], [-v, a, -b], [v, a, b], [v, -a, -b]])
        elif k == "xor":
            clauses.extend([[-v, a, b], [-v, -a, -b], [v, -a, b], [v, a, -b]])
        else:
            raise LogicVerifyError(f"cannot encode {k}")
        return v

    top = encode(root)
    clauses.append([top])
    return clauses, var_to_id


def _dpll_solve(clauses: list[list[int]], var_to_id: dict[str, int]) -> dict[str, bool] | None:
    steps = [0]

    def propagate(cl: list[list[int]], assign: dict[int, bool]) -> tuple[list[list[int]] | None, dict[int, bool]]:
        cur = [list(c) for c in cl]
        asg = dict(assign)
        while True:
            unit = None
            for c in cur:
                if len(c) == 0:
                    return None, asg
                if len(c) == 1:
                    unit = c[0]
                    break
            if unit is None:
                return cur, asg
            var = abs(unit)
            val = unit > 0
            if var in asg and asg[var] != val:
                return None, asg
            asg[var] = val
            nxt: list[list[int]] = []
            for c in cur:
                if unit in c:
                    continue
                reduced = [lit for lit in c if lit != -unit]
                if not reduced:
                    return None, asg
                nxt.append(reduced)
            cur = nxt

    def search(cl: list[list[int]], assign: dict[int, bool]) -> dict[int, bool] | None:
        steps[0] += 1
        if steps[0] > MAX_DPLL_STEPS:
            raise LogicVerifyError(f"SAT solver step limit ({MAX_DPLL_STEPS}) exceeded")
        cur, asg = propagate(cl, assign)
        if cur is None:
            return None
        if not cur:
            return asg
        # Pick literal from shortest clause (MOM heuristic).
        shortest = min(cur, key=len)
        lit = shortest[0]
        res = search(cur + [[lit]], asg)
        if res is not None:
            return res
        return search(cur + [[-lit]], asg)

    raw = search(clauses, {})
    if raw is None:
        return None
    return {name: bool(raw.get(vid, False)) for name, vid in sorted(var_to_id.items())}


def _solve_node_sat(root: _Node) -> tuple[bool, dict[str, bool] | None, list[str]]:
    vars_set: set[str] = set()
    _collect_vars(root, vars_set)
    var_names = sorted(vars_set)
    if len(var_names) > MAX_VARS:
        raise LogicVerifyError(f"formula has {len(var_names)} variables (limit {MAX_VARS})")
    clauses, var_to_id = _tseitin_cnf(root, var_names)
    model = _dpll_solve(clauses, var_to_id)
    return (model is not None), model, var_names


def _to_nnf(node: _Node, negated: bool = False) -> _Node:
    k = node.kind
    if k == "const":
        return _Node("const", value=(not bool(node.value)) if negated else bool(node.value))
    if k == "var":
        return _Node("not", left=node) if negated else node
    if k == "not":
        return _to_nnf(node.left, not negated)  # type: ignore[arg-type]
    if k == "imp":
        # A -> B == ~A | B
        desugared = _Node("or", left=_Node("not", left=node.left), right=node.right)
        return _to_nnf(desugared, negated)
    if k == "iff":
        # A <-> B == (A -> B) & (B -> A)
        desugared = _Node(
            "and",
            left=_Node("imp", left=node.left, right=node.right),
            right=_Node("imp", left=node.right, right=node.left),
        )
        return _to_nnf(desugared, negated)
    if k == "xor":
        desugared = _Node(
            "and",
            left=_Node("or", left=node.left, right=node.right),
            right=_Node("or", left=_Node("not", left=node.left), right=_Node("not", left=node.right)),
        )
        return _to_nnf(desugared, negated)
    if k == "and":
        if negated:
            return _Node("or", left=_to_nnf(node.left, True), right=_to_nnf(node.right, True))  # type: ignore[arg-type]
        return _Node("and", left=_to_nnf(node.left, False), right=_to_nnf(node.right, False))  # type: ignore[arg-type]
    if k == "or":
        if negated:
            return _Node("and", left=_to_nnf(node.left, True), right=_to_nnf(node.right, True))  # type: ignore[arg-type]
        return _Node("or", left=_to_nnf(node.left, False), right=_to_nnf(node.right, False))  # type: ignore[arg-type]
    raise LogicVerifyError(f"unsupported node {k}")


def _nnf_to_cnf_clauses(node: _Node, *, max_clauses: int = 256) -> list[frozenset[str]]:
    k = node.kind
    if k == "const":
        return [] if node.value else [frozenset()]
    if k == "var":
        return [frozenset({str(node.value)})]
    if k == "not" and node.left and node.left.kind == "var":
        return [frozenset({f"!{node.left.value}"})]
    if k == "and":
        left_c = _nnf_to_cnf_clauses(node.left, max_clauses=max_clauses)  # type: ignore[arg-type]
        right_c = _nnf_to_cnf_clauses(node.right, max_clauses=max_clauses)  # type: ignore[arg-type]
        merged = left_c + right_c
        if len(merged) > max_clauses:
            raise LogicVerifyError(f"CNF expansion exceeded {max_clauses} clauses")
        return merged
    if k == "or":
        left_c = _nnf_to_cnf_clauses(node.left, max_clauses=max_clauses)  # type: ignore[arg-type]
        right_c = _nnf_to_cnf_clauses(node.right, max_clauses=max_clauses)  # type: ignore[arg-type]
        out: list[frozenset[str]] = []
        for a in left_c:
            for b in right_c:
                union = a | b
                # Drop tautological clauses containing both X and !X
                if any((lit[1:] if lit.startswith("!") else f"!{lit}") in union for lit in union):
                    continue
                out.append(union)
                if len(out) > max_clauses:
                    raise LogicVerifyError(f"CNF expansion exceeded {max_clauses} clauses")
        return out
    raise LogicVerifyError(f"unexpected NNF node {k}")


# --- Finite-domain CSP solver (MRV + forward checking) -----------------------

def _solve_csp(domains: dict[str, list[Any]], constraints: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(domains, dict) or not domains:
        raise LogicVerifyError("csp requires a non-empty domains dict")
    if len(domains) > MAX_CSP_VARS:
        raise LogicVerifyError(f"csp supports at most {MAX_CSP_VARS} variables")
    clean_domains: dict[str, list[Any]] = {}
    for var, vals in domains.items():
        if not isinstance(vals, list) or not vals:
            raise LogicVerifyError(f"domain for {var!r} must be a non-empty list")
        if len(vals) > MAX_CSP_DOMAIN:
            raise LogicVerifyError(f"domain for {var!r} exceeds {MAX_CSP_DOMAIN} values")
        clean_domains[str(var)] = list(vals)

    norm_constraints: list[dict[str, Any]] = []
    for raw_c in constraints or []:
        if not isinstance(raw_c, dict):
            raise LogicVerifyError("each CSP constraint must be an object")
        op = str(raw_c.get("op") or "").strip().lower()
        c_vars = [str(v) for v in (raw_c.get("vars") or [])]
        if not c_vars or any(v not in clean_domains for v in c_vars):
            raise LogicVerifyError(f"constraint references unknown variable: {raw_c!r}")
        if op not in {"alldiff", "==", "!=", "<", "<=", ">", ">=", "sum_eq", "sum_le", "sum_ge"}:
            raise LogicVerifyError(f"unsupported CSP constraint op: {op!r}")
        norm_constraints.append({"op": op, "vars": c_vars, "value": raw_c.get("value")})

    def _check_constraint(c: dict[str, Any], assign: dict[str, Any]) -> bool:
        op = c["op"]
        vs = c["vars"]
        assigned_vals = [assign[v] for v in vs if v in assign]
        if op == "alldiff":
            return len(assigned_vals) == len(set(assigned_vals))
        if len(assigned_vals) < len(vs):
            return True
        if op in {"==", "!=", "<", "<=", ">", ">="}:
            if len(vs) != 2:
                raise LogicVerifyError(f"binary constraint {op!r} requires exactly 2 vars")
            a, b = assign[vs[0]], assign[vs[1]]
            if op == "==":
                return a == b
            if op == "!=":
                return a != b
            if op == "<":
                return a < b
            if op == "<=":
                return a <= b
            if op == ">":
                return a > b
            return a >= b
        total = sum(assigned_vals)
        target = c["value"]
        if op == "sum_eq":
            return total == target
        if op == "sum_le":
            return total <= target
        return total >= target

    steps = [0]

    def backtrack(assign: dict[str, Any], doms: dict[str, list[Any]]) -> dict[str, Any] | None:
        steps[0] += 1
        if steps[0] > MAX_CSP_STEPS:
            raise LogicVerifyError(f"CSP solver step limit ({MAX_CSP_STEPS}) exceeded")
        if len(assign) == len(clean_domains):
            return dict(assign)
        # MRV heuristic: choose unassigned variable with smallest remaining domain
        unassigned = [v for v in clean_domains if v not in assign]
        var = min(unassigned, key=lambda v: len(doms[v]))
        for val in doms[var]:
            assign[var] = val
            ok = all(_check_constraint(c, assign) for c in norm_constraints if var in c["vars"])
            if ok:
                # Forward checking on binary/alldiff neighbors
                new_doms = {k: list(vals) for k, vals in doms.items()}
                wipeout = False
                for other in unassigned:
                    if other == var:
                        continue
                    filtered = []
                    for cand in new_doms[other]:
                        assign[other] = cand
                        if all(_check_constraint(c, assign) for c in norm_constraints if other in c["vars"]):
                            filtered.append(cand)
                        del assign[other]
                    if not filtered:
                        wipeout = True
                        break
                    new_doms[other] = filtered
                if not wipeout:
                    sol = backtrack(assign, new_doms)
                    if sol is not None:
                        return sol
            del assign[var]
        return None

    solution = backtrack({}, clean_domains)
    return {
        "ok": True,
        "action": "csp",
        "satisfiable": solution is not None,
        "solution": solution,
        "steps": steps[0],
    }


def logic_verify(
    formula: str = "",
    *,
    action: str = "satisfiable",
    formula_b: str = "",
    premises: list[str] | None = None,
    conclusion: str = "",
    domains: dict[str, list[Any]] | None = None,
    constraints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify propositional formulas (SAT/equivalence/implication/truth table/CNF) or solve finite-domain CSPs."""
    act = str(action or "satisfiable").strip().lower() or "satisfiable"
    try:
        if act == "satisfiable":
            root = _parse_formula(formula)
            sat, model, var_names = _solve_node_sat(root)
            # Also check tautology: is NOT(formula) unsatisfiable?
            not_sat, counterexample, _ = _solve_node_sat(_Node("not", left=root))
            return {
                "ok": True,
                "action": "satisfiable",
                "variables": var_names,
                "satisfiable": sat,
                "tautology": not not_sat,
                "contradiction": not sat,
                "witness": model,
                "falsifying_assignment": counterexample,
            }

        if act == "truth_table":
            root = _parse_formula(formula)
            vars_set: set[str] = set()
            _collect_vars(root, vars_set)
            var_names = sorted(vars_set)
            if len(var_names) > MAX_TRUTH_TABLE_VARS:
                raise LogicVerifyError(
                    f"truth_table supports at most {MAX_TRUTH_TABLE_VARS} variables (got {len(var_names)}); use action='satisfiable'"
                )
            rows = []
            true_count = 0
            for mask in range(1 << len(var_names)):
                assign = {var_names[i]: bool((mask >> (len(var_names) - 1 - i)) & 1) for i in range(len(var_names))}
                res = _eval_node(root, assign)
                if res:
                    true_count += 1
                rows.append({"assignment": assign, "result": res})
            return {
                "ok": True,
                "action": "truth_table",
                "variables": var_names,
                "row_count": len(rows),
                "true_count": true_count,
                "tautology": true_count == len(rows),
                "contradiction": true_count == 0,
                "rows": rows,
            }

        if act == "equivalent":
            node_a = _parse_formula(formula)
            node_b = _parse_formula(formula_b)
            # Equivalent iff (A xor B) is unsatisfiable.
            diff_node = _Node("xor", left=node_a, right=node_b)
            has_diff, counterexample, var_names = _solve_node_sat(diff_node)
            out: dict[str, Any] = {
                "ok": True,
                "action": "equivalent",
                "variables": var_names,
                "equivalent": not has_diff,
                "counterexample": counterexample,
            }
            if counterexample is not None:
                out["formula_a_value"] = _eval_node(node_a, counterexample)
                out["formula_b_value"] = _eval_node(node_b, counterexample)
            return out

        if act == "implies":
            prem_list = [p for p in (premises or []) if str(p or "").strip()]
            if not prem_list and formula:
                prem_list = [formula]
            if not prem_list or not str(conclusion or "").strip():
                raise LogicVerifyError("implies requires non-empty premises (or formula) and conclusion")
            prem_nodes = [_parse_formula(p) for p in prem_list]
            conj = prem_nodes[0]
            for pn in prem_nodes[1:]:
                conj = _Node("and", left=conj, right=pn)
            conc_node = _parse_formula(conclusion)
            # Counterexample: premises AND NOT conclusion
            refute = _Node("and", left=conj, right=_Node("not", left=conc_node))
            refutable, counterexample, var_names = _solve_node_sat(refute)
            return {
                "ok": True,
                "action": "implies",
                "variables": var_names,
                "valid": not refutable,
                "counterexample": counterexample,
            }

        if act == "cnf":
            root = _parse_formula(formula)
            nnf = _to_nnf(root, False)
            raw_clauses = _nnf_to_cnf_clauses(nnf)
            # Remove duplicate & subsumed clauses
            unique = sorted({tuple(sorted(c)) for c in raw_clauses}, key=lambda c: (len(c), c))
            minimal: list[list[str]] = []
            for cl in unique:
                cl_set = set(cl)
                if any(set(prev).issubset(cl_set) for prev in minimal):
                    continue
                minimal.append(list(cl))
            rendered = " & ".join(
                ("(" + " | ".join(cl) + ")" if len(cl) > 1 else (cl[0] if cl else "False"))
                for cl in minimal
            ) or "True"
            return {
                "ok": True,
                "action": "cnf",
                "clause_count": len(minimal),
                "clauses": minimal,
                "cnf": rendered,
            }

        if act == "csp":
            return _solve_csp(domains or {}, constraints or [])

        raise LogicVerifyError(
            f"unknown action {act!r}; expected satisfiable, truth_table, equivalent, implies, cnf, or csp"
        )
    except LogicVerifyError as exc:
        return {"ok": False, "action": act, "error": str(exc)}


__all__ = ["LogicVerifyError", "logic_verify"]
