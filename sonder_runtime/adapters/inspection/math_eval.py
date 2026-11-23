"""Pure, bounded, stdlib-only exact math, algebra, matrix, statistics, and unit engine.

Designed to eliminate arithmetic, algebra, combinatorics, statistics, matrix,
unit-conversion, and date-math hallucinations in local and served chat turns
without spawning a subprocess or requiring execution approval.

Security invariants:
- Never calls ``eval()`` or ``exec()``; parses expressions via ``ast.parse`` and
  walks a strict closed whitelist of AST nodes and pure math functions.
- Enforces hard ceilings on expression length, AST node count, nesting depth,
  exponent magnitude, factorial/combination size, prime-factorization steps,
  matrix dimensions, and numeric solver iterations.
- Zero filesystem, network, or environment access.
"""
from __future__ import annotations

import ast
import cmath
import datetime as _dt
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
import math
import re
import statistics
from typing import Any

MAX_EXPRESSION_CHARS = 4_000
MAX_AST_NODES = 300
MAX_AST_DEPTH = 32
MAX_EXPONENT = 10_000
MAX_FACTORIAL = 2_000
MAX_MATRIX_DIM = 32
MAX_FACTORIZE_ABS = 10**18
MAX_FACTORIZE_STEPS = 100_000
MAX_SOLVER_STEPS = 2_000
MAX_PRECISION = 50
MIN_PRECISION = 2


class MathEvalError(ValueError):
    """Raised on syntax, domain, or safety-bound errors in math evaluation."""


def _check_ast_bounds(tree: ast.AST) -> None:
    nodes = 0
    stack: list[tuple[ast.AST, int]] = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_AST_NODES:
            raise MathEvalError(f"expression too complex (>{MAX_AST_NODES} AST nodes)")
        if depth > MAX_AST_DEPTH:
            raise MathEvalError(f"expression nested too deeply (>{MAX_AST_DEPTH} levels)")
        for child in ast.iter_child_nodes(node):
            stack.append((child, depth + 1))


def _is_prime_int(n: int) -> bool:
    if n < 2:
        return False
    small = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    for p in small:
        if n == p:
            return True
        if n % p == 0:
            return False
    # Deterministic Miller-Rabin for all 64-bit integers.
    d = n - 1
    s = 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if a >= n:
            continue
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _factorize_int(n: int) -> dict[int, int]:
    if abs(n) > MAX_FACTORIZE_ABS:
        raise MathEvalError(f"factorize input exceeds limit (|n| <= {MAX_FACTORIZE_ABS})")
    if n == 0:
        raise MathEvalError("cannot factorize 0")
    val = abs(n)
    factors: dict[int, int] = {}
    if val == 1:
        return factors
    for d in (2, 3, 5):
        while val % d == 0:
            factors[d] = factors.get(d, 0) + 1
            val //= d
    step = 0
    d = 7
    inc = (4, 2, 4, 2, 4, 6, 2, 6)
    idx = 0
    while d * d <= val:
        step += 1
        if step > MAX_FACTORIZE_STEPS:
            if _is_prime_int(val):
                factors[val] = factors.get(val, 0) + 1
                return factors
            raise MathEvalError("factorization step limit exceeded for large composite integer")
        if val % d == 0:
            while val % d == 0:
                factors[d] = factors.get(d, 0) + 1
                val //= d
        d += inc[idx]
        idx = (idx + 1) & 7
    if val > 1:
        factors[val] = factors.get(val, 0) + 1
    return factors


def _totient_int(n: int) -> int:
    if n < 1:
        raise MathEvalError("totient requires n >= 1")
    if n == 1:
        return 1
    result = n
    for p in _factorize_int(n):
        result = result // p * (p - 1)
    return result


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise MathEvalError(f"{name} expects an integer, not bool")
    if isinstance(value, int):
        return value
    if isinstance(value, Fraction) and value.denominator == 1:
        return int(value.numerator)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Decimal) and value == int(value):
        return int(value)
    raise MathEvalError(f"{name} expects an integer")


def _as_float(value: Any) -> float:
    if isinstance(value, complex):
        if abs(value.imag) > 1e-12:
            raise MathEvalError("expected real number, got complex")
        return float(value.real)
    return float(value)


def _as_fraction(value: Any) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Fraction(value)
    if isinstance(value, Decimal):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MathEvalError("non-finite float")
        return Fraction(str(value))
    raise MathEvalError(f"cannot convert {type(value).__name__} to exact rational")


def _safe_pow(base: Any, exp: Any) -> Any:
    if isinstance(exp, (int, float, Fraction, Decimal)) and not isinstance(exp, bool):
        if abs(float(exp)) > MAX_EXPONENT:
            raise MathEvalError(f"exponent magnitude exceeds {MAX_EXPONENT}")
    if isinstance(base, int) and isinstance(exp, int) and not isinstance(base, bool) and not isinstance(exp, bool):
        if abs(base) > 10**6 and abs(exp) > 200:
            raise MathEvalError("power result would exceed safety bound")
        if exp < 0:
            return Fraction(1, base ** (-exp))
        return base ** exp
    if isinstance(base, Fraction) and isinstance(exp, int) and not isinstance(exp, bool):
        if abs(exp) > 500:
            raise MathEvalError("rational exponent magnitude exceeds 500")
        return base ** exp
    try:
        return base ** exp
    except OverflowError as exc:
        raise MathEvalError(f"numeric overflow in exponentiation: {exc}") from exc


def _normal_pdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    if sigma <= 0:
        raise MathEvalError("sigma must be > 0")
    z = (x - mu) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))


def _normal_cdf(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    if sigma <= 0:
        raise MathEvalError("sigma must be > 0")
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def _binom_pmf(k: int, n: int, p: float) -> float:
    k_i = _as_int(k, "k")
    n_i = _as_int(n, "n")
    if n_i < 0 or n_i > MAX_FACTORIAL or k_i < 0 or k_i > n_i:
        raise MathEvalError("invalid binomial parameters")
    if not (0.0 <= p <= 1.0):
        raise MathEvalError("p must be in [0, 1]")
    return math.comb(n_i, k_i) * (p ** k_i) * ((1.0 - p) ** (n_i - k_i))


def _binom_cdf(k: int, n: int, p: float) -> float:
    k_i = _as_int(k, "k")
    n_i = _as_int(n, "n")
    if n_i < 0 or n_i > MAX_FACTORIAL:
        raise MathEvalError("invalid binomial n")
    if k_i < 0:
        return 0.0
    if k_i >= n_i:
        return 1.0
    return sum(_binom_pmf(i, n_i, p) for i in range(k_i + 1))


def _poisson_pmf(k: int, lam: float) -> float:
    k_i = _as_int(k, "k")
    if k_i < 0 or k_i > MAX_FACTORIAL or lam < 0:
        raise MathEvalError("invalid poisson parameters")
    if lam == 0:
        return 1.0 if k_i == 0 else 0.0
    return math.exp(k_i * math.log(lam) - lam - math.lgamma(k_i + 1))


def _poisson_cdf(k: int, lam: float) -> float:
    k_i = _as_int(k, "k")
    if k_i < 0:
        return 0.0
    if k_i > MAX_FACTORIAL or lam < 0:
        raise MathEvalError("invalid poisson parameters")
    return sum(_poisson_pmf(i, lam) for i in range(k_i + 1))


def _linreg(xs: list[float], ys: list[float]) -> dict[str, float]:
    if len(xs) != len(ys) or len(xs) < 2:
        raise MathEvalError("linreg requires two equal-length lists of length >= 2")
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise MathEvalError("linreg requires non-constant x values")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    intercept = my - slope * mx
    r2 = 1.0 if syy == 0 else (sxy * sxy) / (sxx * syy)
    return {
        "slope": slope,
        "intercept": intercept,
        "r_squared": r2,
        "r": math.copysign(math.sqrt(max(0.0, r2)), sxy),
    }


def _build_safe_funcs(exact: bool) -> dict[str, Any]:
    def _fact(x: Any) -> int:
        n = _as_int(x, "factorial")
        if n < 0 or n > MAX_FACTORIAL:
            raise MathEvalError(f"factorial requires 0 <= n <= {MAX_FACTORIAL}")
        return math.factorial(n)

    def _comb(n: Any, k: Any) -> int:
        ni, ki = _as_int(n, "comb"), _as_int(k, "comb")
        if ni < 0 or ki < 0 or ni > 10_000:
            raise MathEvalError("comb arguments out of bounds (0 <= n <= 10000)")
        return math.comb(ni, ki)

    def _perm(n: Any, k: Any = None) -> int:
        ni = _as_int(n, "perm")
        if ni < 0 or ni > MAX_FACTORIAL:
            raise MathEvalError(f"perm requires 0 <= n <= {MAX_FACTORIAL}")
        if k is None:
            return math.perm(ni)
        ki = _as_int(k, "perm")
        if ki < 0 or ki > ni:
            raise MathEvalError("perm requires 0 <= k <= n")
        return math.perm(ni, ki)

    def _sqrt(x: Any) -> Any:
        if isinstance(x, complex) or (not isinstance(x, complex) and float(x) < 0):
            return cmath.sqrt(x)
        return math.sqrt(float(x))

    def _log(x: Any, base: Any = None) -> Any:
        xv = _as_float(x)
        if xv <= 0:
            return cmath.log(x) if base is None else cmath.log(x, base)
        return math.log(xv) if base is None else math.log(xv, _as_float(base))

    def _mod_pow(a: Any, b: Any, m: Any) -> int:
        ai, bi, mi = _as_int(a, "mod_pow"), _as_int(b, "mod_pow"), _as_int(m, "mod_pow")
        if mi == 0:
            raise MathEvalError("mod_pow modulus cannot be 0")
        return pow(ai, bi, mi)

    def _mod_inv(a: Any, m: Any) -> int:
        ai, mi = _as_int(a, "mod_inv"), _as_int(m, "mod_inv")
        if mi <= 0:
            raise MathEvalError("mod_inv modulus must be > 0")
        return pow(ai, -1, mi)

    def _frac(a: Any, b: Any = None) -> Fraction:
        if b is None:
            return _as_fraction(a)
        return Fraction(_as_int(a, "frac"), _as_int(b, "frac"))

    def _seq(args: tuple[Any, ...]) -> list[float]:
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            vals = args[0]
        else:
            vals = args
        if not vals:
            raise MathEvalError("expected at least one numeric value")
        return [_as_float(v) for v in vals]

    return {
        "abs": abs,
        "round": round,
        "min": lambda *a: min(a[0]) if len(a) == 1 and isinstance(a[0], (list, tuple)) else min(a),
        "max": lambda *a: max(a[0]) if len(a) == 1 and isinstance(a[0], (list, tuple)) else max(a),
        "sum": lambda *a: sum(a[0]) if len(a) == 1 and isinstance(a[0], (list, tuple)) else sum(a),
        "len": lambda x: len(x),
        "floor": lambda x: math.floor(_as_fraction(x) if exact else _as_float(x)),
        "ceil": lambda x: math.ceil(_as_fraction(x) if exact else _as_float(x)),
        "trunc": lambda x: math.trunc(_as_fraction(x) if exact else _as_float(x)),
        "sqrt": _sqrt,
        "cbrt": lambda x: math.cbrt(_as_float(x)),
        "exp": lambda x: cmath.exp(x) if isinstance(x, complex) else math.exp(_as_float(x)),
        "log": _log,
        "ln": lambda x: _log(x),
        "log2": lambda x: math.log2(_as_float(x)),
        "log10": lambda x: math.log10(_as_float(x)),
        "sin": lambda x: cmath.sin(x) if isinstance(x, complex) else math.sin(_as_float(x)),
        "cos": lambda x: cmath.cos(x) if isinstance(x, complex) else math.cos(_as_float(x)),
        "tan": lambda x: cmath.tan(x) if isinstance(x, complex) else math.tan(_as_float(x)),
        "asin": lambda x: math.asin(_as_float(x)),
        "acos": lambda x: math.acos(_as_float(x)),
        "atan": lambda x: math.atan(_as_float(x)),
        "atan2": lambda y, x: math.atan2(_as_float(y), _as_float(x)),
        "sinh": lambda x: math.sinh(_as_float(x)),
        "cosh": lambda x: math.cosh(_as_float(x)),
        "tanh": lambda x: math.tanh(_as_float(x)),
        "degrees": lambda x: math.degrees(_as_float(x)),
        "radians": lambda x: math.radians(_as_float(x)),
        "hypot": lambda *a: math.hypot(*(_as_float(v) for v in a)),
        "gamma": lambda x: math.gamma(_as_float(x)),
        "lgamma": lambda x: math.lgamma(_as_float(x)),
        "erf": lambda x: math.erf(_as_float(x)),
        "erfc": lambda x: math.erfc(_as_float(x)),
        "factorial": _fact,
        "comb": _comb,
        "perm": _perm,
        "gcd": lambda *a: math.gcd(*(_as_int(v, "gcd") for v in a)),
        "lcm": lambda *a: math.lcm(*(_as_int(v, "lcm") for v in a)),
        "is_prime": lambda x: _is_prime_int(_as_int(x, "is_prime")),
        "factorize": lambda x: _factorize_int(_as_int(x, "factorize")),
        "totient": lambda x: _totient_int(_as_int(x, "totient")),
        "mod_pow": _mod_pow,
        "mod_inv": _mod_inv,
        "frac": _frac,
        "hex": lambda x: hex(_as_int(x, "hex")),
        "bin": lambda x: bin(_as_int(x, "bin")),
        "oct": lambda x: oct(_as_int(x, "oct")),
        "mean": lambda *a: statistics.fmean(_seq(a)),
        "median": lambda *a: statistics.median(_seq(a)),
        "stdev": lambda *a: statistics.stdev(_seq(a)),
        "pstdev": lambda *a: statistics.pstdev(_seq(a)),
        "variance": lambda *a: statistics.variance(_seq(a)),
        "pvariance": lambda *a: statistics.pvariance(_seq(a)),
        "quantiles": lambda xs, n=4: statistics.quantiles([_as_float(v) for v in xs], n=_as_int(n, "n")),
        "corr": lambda xs, ys: statistics.correlation([_as_float(v) for v in xs], [_as_float(v) for v in ys]),
        "linreg": lambda xs, ys: _linreg([_as_float(v) for v in xs], [_as_float(v) for v in ys]),
        "normal_pdf": lambda x, mu=0.0, sigma=1.0: _normal_pdf(_as_float(x), _as_float(mu), _as_float(sigma)),
        "normal_cdf": lambda x, mu=0.0, sigma=1.0: _normal_cdf(_as_float(x), _as_float(mu), _as_float(sigma)),
        "binom_pmf": lambda k, n, p: _binom_pmf(k, n, _as_float(p)),
        "binom_cdf": lambda k, n, p: _binom_cdf(k, n, _as_float(p)),
        "poisson_pmf": lambda k, lam: _poisson_pmf(k, _as_float(lam)),
        "poisson_cdf": lambda k, lam: _poisson_cdf(k, _as_float(lam)),
    }


_CONSTANTS: dict[str, Any] = {
    "pi": math.pi,
    "tau": math.tau,
    "e": math.e,
    "phi": (1.0 + math.sqrt(5.0)) / 2.0,
    "inf": math.inf,
    "j": 1j,
    "i": 1j,
    "True": True,
    "False": False,
}


def _preprocess_expression(expr: str) -> str:
    text = str(expr or "").strip()
    if not text:
        raise MathEvalError("expression is required")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise MathEvalError(f"expression exceeds {MAX_EXPRESSION_CHARS} characters")
    # Support common math caret power only when user did not write bitwise op?
    # Keep ^ as Python XOR in bitwise expressions unless obvious math? Actually keep standard Python ** and support Unicode operators.
    text = text.replace("\u00d7", "*").replace("\u00f7", "/").replace("\u2212", "-").replace("\u03c0", "pi")
    return text


def _eval_ast(
    node: ast.AST,
    env: dict[str, Any],
    funcs: dict[str, Any],
    *,
    exact: bool = False,
) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, env, funcs, exact=exact)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, complex, bool)):
            if exact and isinstance(node.value, float):
                return Fraction(str(node.value))
            return node.value
        raise MathEvalError(f"unsupported literal type: {type(node.value).__name__}")
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise MathEvalError(f"unknown identifier: {node.id!r}")
    if isinstance(node, (ast.List, ast.Tuple)):
        if len(node.elts) > 256:
            raise MathEvalError("sequence literal exceeds 256 items")
        return [_eval_ast(el, env, funcs, exact=exact) for el in node.elts]
    if isinstance(node, ast.UnaryOp):
        val = _eval_ast(node.operand, env, funcs, exact=exact)
        if isinstance(node.op, ast.UAdd):
            return +val
        if isinstance(node.op, ast.USub):
            return -val
        if isinstance(node.op, ast.Invert):
            return ~_as_int(val, "~")
        if isinstance(node.op, ast.Not):
            return not bool(val)
        raise MathEvalError(f"unsupported unary operator: {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        left = _eval_ast(node.left, env, funcs, exact=exact)
        right = _eval_ast(node.right, env, funcs, exact=exact)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            if right == 0:
                raise MathEvalError("division by zero")
            if exact and isinstance(left, (int, Fraction)) and isinstance(right, (int, Fraction)):
                return Fraction(left) / Fraction(right)
            return left / right
        if isinstance(op, ast.FloorDiv):
            if right == 0:
                raise MathEvalError("division by zero")
            return left // right
        if isinstance(op, ast.Mod):
            if right == 0:
                raise MathEvalError("modulo by zero")
            return left % right
        if isinstance(op, ast.Pow):
            return _safe_pow(left, right)
        if isinstance(op, ast.BitAnd):
            return _as_int(left, "&") & _as_int(right, "&")
        if isinstance(op, ast.BitOr):
            return _as_int(left, "|") | _as_int(right, "|")
        if isinstance(op, ast.BitXor):
            return _as_int(left, "^") ^ _as_int(right, "^")
        if isinstance(op, ast.LShift):
            shift = _as_int(right, "<<")
            if shift < 0 or shift > 1024:
                raise MathEvalError("shift count out of bounds (0..1024)")
            return _as_int(left, "<<") << shift
        if isinstance(op, ast.RShift):
            shift = _as_int(right, ">>")
            if shift < 0 or shift > 1024:
                raise MathEvalError("shift count out of bounds (0..1024)")
            return _as_int(left, ">>") >> shift
        raise MathEvalError(f"unsupported operator: {type(op).__name__}")
    if isinstance(node, ast.Compare):
        left = _eval_ast(node.left, env, funcs, exact=exact)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval_ast(comp, env, funcs, exact=exact)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            else:
                raise MathEvalError(f"unsupported comparison: {type(op).__name__}")
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            res: Any = True
            for val_node in node.values:
                res = _eval_ast(val_node, env, funcs, exact=exact)
                if not res:
                    return res
            return res
        if isinstance(node.op, ast.Or):
            res = False
            for val_node in node.values:
                res = _eval_ast(val_node, env, funcs, exact=exact)
                if res:
                    return res
            return res
        raise MathEvalError(f"unsupported boolean operator: {type(node.op).__name__}")
    if isinstance(node, ast.IfExp):
        cond = _eval_ast(node.test, env, funcs, exact=exact)
        return _eval_ast(node.body if cond else node.orelse, env, funcs, exact=exact)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise MathEvalError("only direct named math function calls are allowed")
        fn_name = node.func.id
        if fn_name not in funcs:
            raise MathEvalError(f"unknown math function: {fn_name!r}")
        if node.keywords:
            kwargs = {kw.arg: _eval_ast(kw.value, env, funcs, exact=exact) for kw in node.keywords if kw.arg}
        else:
            kwargs = {}
        args = [_eval_ast(arg, env, funcs, exact=exact) for arg in node.args]
        try:
            return funcs[fn_name](*args, **kwargs)
        except MathEvalError:
            raise
        except (ValueError, TypeError, ZeroDivisionError, OverflowError, statistics.StatisticsError) as exc:
            raise MathEvalError(f"{fn_name}() failed: {exc}") from exc
    raise MathEvalError(f"disallowed syntax: {type(node).__name__}")


def _eval_decimal_ast(node: ast.AST, env: dict[str, Decimal]) -> Decimal:
    """High-precision Decimal evaluator for arithmetic/algebraic expressions."""
    if isinstance(node, ast.Expression):
        return _eval_decimal_ast(node.body, env)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Decimal(str(node.value))
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id == "pi":
            return Decimal("3.14159265358979323846264338327950288419716939937510")
        if node.id == "e":
            return Decimal("2.71828182845904523536028747135266249775724709369995")
        if node.id == "tau":
            return Decimal("6.28318530717958647692528676655900576839433879875021")
        raise MathEvalError(f"unknown decimal identifier: {node.id!r}")
    if isinstance(node, ast.UnaryOp):
        v = _eval_decimal_ast(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return +v
        if isinstance(node.op, ast.USub):
            return -v
        raise MathEvalError("unsupported decimal unary op")
    if isinstance(node, ast.BinOp):
        l = _eval_decimal_ast(node.left, env)
        r = _eval_decimal_ast(node.right, env)
        if isinstance(node.op, ast.Add):
            return l + r
        if isinstance(node.op, ast.Sub):
            return l - r
        if isinstance(node.op, ast.Mult):
            return l * r
        if isinstance(node.op, ast.Div):
            if r == 0:
                raise MathEvalError("division by zero")
            return l / r
        if isinstance(node.op, ast.FloorDiv):
            if r == 0:
                raise MathEvalError("division by zero")
            return l // r
        if isinstance(node.op, ast.Mod):
            if r == 0:
                raise MathEvalError("modulo by zero")
            return l % r
        if isinstance(node.op, ast.Pow):
            if abs(r) > MAX_EXPONENT:
                raise MathEvalError(f"exponent magnitude exceeds {MAX_EXPONENT}")
            return l ** r
        raise MathEvalError("unsupported decimal binary op")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = node.func.id
        args = [_eval_decimal_ast(a, env) for a in node.args]
        if fn == "sqrt" and len(args) == 1:
            return args[0].sqrt()
        if fn == "exp" and len(args) == 1:
            return args[0].exp()
        if fn == "ln" and len(args) == 1:
            return args[0].ln()
        if fn == "log10" and len(args) == 1:
            return args[0].log10()
        if fn == "abs" and len(args) == 1:
            return abs(args[0])
        raise MathEvalError(f"decimal precision mode does not support {fn}()")
    raise MathEvalError(f"unsupported decimal syntax: {type(node).__name__}")


def _format_value(value: Any) -> Any:
    if isinstance(value, Fraction):
        return {
            "exact": f"{value.numerator}/{value.denominator}" if value.denominator != 1 else str(value.numerator),
            "float": float(value),
        }
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag, "text": f"{value.real:g}{value.imag:+g}j"}
    if isinstance(value, dict):
        return {str(k): _format_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_format_value(v) for v in value]
    return value


# --- Exact linear / polynomial / numerical equation solving ------------------

def _split_equation(eq: str) -> tuple[str, str]:
    text = _preprocess_expression(eq)
    if "=" in text and "==" not in text and "!=" not in text and "<=" not in text and ">=" not in text:
        lhs, rhs = text.split("=", 1)
        return lhs.strip(), rhs.strip()
    return text, "0"


def _eval_fn_at(expr: str, var: str, x: float, env: dict[str, Any]) -> float:
    tree = ast.parse(expr, mode="eval")
    _check_ast_bounds(tree)
    funcs = _build_safe_funcs(exact=False)
    local_env = dict(env)
    local_env[var] = x
    val = _eval_ast(tree, local_env, funcs, exact=False)
    return _as_float(val)


def _solve_1d_equation(
    equation: str,
    variable: str = "x",
    *,
    bracket: tuple[float, float] | None = None,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lhs, rhs = _split_equation(equation)
    expr = f"({lhs}) - ({rhs})"
    tree = ast.parse(expr, mode="eval")
    _check_ast_bounds(tree)
    funcs = _build_safe_funcs(exact=False)
    base_env = dict(variables or {})

    def f(x: float) -> float:
        env = dict(base_env)
        env[variable] = x
        return _as_float(_eval_ast(tree, env, funcs, exact=False))

    # First test if f(x) is exact degree-1 or degree-2 polynomial via finite differences.
    try:
        f0 = f(0.0)
        f1 = f(1.0)
        fm1 = f(-1.0)
        f2 = f(2.0)
        f3 = f(3.0)
        # Quadratic coefficients: a*x^2 + b*x + c
        c = f0
        a = 0.5 * (f1 + fm1) - c
        b = 0.5 * (f1 - fm1)
        is_poly2 = (
            abs((a * 4 + b * 2 + c) - f2) < 1e-8 * max(1.0, abs(f2))
            and abs((a * 9 + b * 3 + c) - f3) < 1e-8 * max(1.0, abs(f3))
        )
        if is_poly2:
            if abs(a) < 1e-12:
                if abs(b) < 1e-12:
                    if abs(c) < 1e-12:
                        return {"ok": True, "kind": "identity", "variable": variable, "solutions": ["all_reals"]}
                    return {"ok": True, "kind": "no_solution", "variable": variable, "solutions": []}
                # Try exact Fraction if possible
                try:
                    ex_funcs = _build_safe_funcs(exact=True)
                    ef0 = _as_fraction(_eval_ast(tree, {**base_env, variable: Fraction(0)}, ex_funcs, exact=True))
                    ef1 = _as_fraction(_eval_ast(tree, {**base_env, variable: Fraction(1)}, ex_funcs, exact=True))
                    slope = ef1 - ef0
                    if slope != 0:
                        root_f = -ef0 / slope
                        return {
                            "ok": True,
                            "kind": "linear",
                            "variable": variable,
                            "solutions": [_format_value(root_f)],
                            "residual": abs(f(float(root_f))),
                        }
                except MathEvalError:
                    pass
                root = -c / b
                return {"ok": True, "kind": "linear", "variable": variable, "solutions": [root], "residual": abs(f(root))}
            disc = b * b - 4.0 * a * c
            if disc >= 0.0:
                s = math.sqrt(disc)
                r1 = (-b - s) / (2.0 * a)
                r2 = (-b + s) / (2.0 * a)
                sols = [r1] if abs(r1 - r2) < 1e-12 else sorted([r1, r2])
                return {
                    "ok": True,
                    "kind": "quadratic",
                    "variable": variable,
                    "solutions": sols,
                    "discriminant": disc,
                    "residuals": [abs(f(r)) for r in sols],
                }
            s = math.sqrt(-disc)
            r1_c = complex(-b / (2.0 * a), -s / (2.0 * a))
            r2_c = complex(-b / (2.0 * a), s / (2.0 * a))
            return {
                "ok": True,
                "kind": "quadratic_complex",
                "variable": variable,
                "solutions": [_format_value(r1_c), _format_value(r2_c)],
                "discriminant": disc,
            }
    except (MathEvalError, ValueError, ZeroDivisionError, OverflowError):
        pass

    # Numerical scan + bisection + secant refinement.
    lo, hi = bracket if bracket is not None else (-100.0, 100.0)
    if lo >= hi:
        raise MathEvalError("bracket must satisfy low < high")
    samples = 400
    step = (hi - lo) / samples
    roots: list[float] = []

    def _safe_f(x: float) -> float | None:
        try:
            v = f(x)
            return v if math.isfinite(v) else None
        except (MathEvalError, ValueError, ZeroDivisionError, OverflowError):
            return None

    prev_x = lo
    prev_y = _safe_f(prev_x)
    for i in range(1, samples + 1):
        curr_x = lo + i * step
        curr_y = _safe_f(curr_x)
        if curr_y is not None and abs(curr_y) < 1e-11:
            if not any(abs(curr_x - r) < 1e-6 for r in roots):
                roots.append(curr_x)
        elif prev_y is not None and curr_y is not None and prev_y * curr_y < 0.0:
            a_pt, b_pt = prev_x, curr_x
            fa = prev_y
            for _ in range(80):
                mid = 0.5 * (a_pt + b_pt)
                fm = _safe_f(mid)
                if fm is None:
                    break
                if abs(fm) < 1e-13 or abs(b_pt - a_pt) < 1e-13:
                    a_pt = b_pt = mid
                    break
                if fa * fm <= 0.0:
                    b_pt = mid
                else:
                    a_pt = mid
                    fa = fm
            root = 0.5 * (a_pt + b_pt)
            if not any(abs(root - r) < 1e-6 for r in roots):
                roots.append(root)
        prev_x, prev_y = curr_x, curr_y

    return {
        "ok": True,
        "kind": "numerical",
        "variable": variable,
        "bracket": [lo, hi],
        "solutions": [round(r, 12) for r in roots],
        "residuals": [abs(_safe_f(r) or 0.0) for r in roots],
    }


def _solve_linear_system(equations: list[str], variables: list[str]) -> dict[str, Any]:
    if not equations or not variables:
        raise MathEvalError("solve_system requires non-empty equations and variables")
    if len(variables) > MAX_MATRIX_DIM or len(equations) > MAX_MATRIX_DIM:
        raise MathEvalError(f"linear system exceeds max dimension {MAX_MATRIX_DIM}")
    n = len(variables)
    if len(equations) != n:
        raise MathEvalError(f"expected {n} equations for {n} variables, got {len(equations)}")
    ex_funcs = _build_safe_funcs(exact=True)
    aug: list[list[Fraction]] = []
    for eq in equations:
        lhs, rhs = _split_equation(eq)
        expr = f"({lhs}) - ({rhs})"
        tree = ast.parse(expr, mode="eval")
        _check_ast_bounds(tree)
        zeros = {v: Fraction(0) for v in variables}
        c0 = _as_fraction(_eval_ast(tree, zeros, ex_funcs, exact=True))
        row: list[Fraction] = []
        for var in variables:
            probe = dict(zeros)
            probe[var] = Fraction(1)
            cv = _as_fraction(_eval_ast(tree, probe, ex_funcs, exact=True)) - c0
            row.append(cv)
        # Verify linearity with a sum-of-ones probe.
        ones = {v: Fraction(1) for v in variables}
        val_ones = _as_fraction(_eval_ast(tree, ones, ex_funcs, exact=True))
        if val_ones != c0 + sum(row):
            raise MathEvalError(f"equation is non-linear: {eq!r}")
        row.append(-c0)
        aug.append(row)

    # Exact Gaussian elimination with partial pivoting.
    for col in range(n):
        pivot = None
        for r in range(col, n):
            if aug[r][col] != 0:
                pivot = r
                break
        if pivot is None:
            return {"ok": False, "error": "system is singular (no unique solution)"}
        aug[col], aug[pivot] = aug[pivot], aug[col]
        piv_val = aug[col][col]
        aug[col] = [x / piv_val for x in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0:
                factor = aug[r][col]
                aug[r] = [aug[r][j] - factor * aug[col][j] for j in range(n + 1)]

    sol = {variables[i]: _format_value(aug[i][n]) for i in range(n)}
    return {"ok": True, "kind": "linear_system", "solution": sol}


# --- Matrix & vector algebra (exact Fraction) --------------------------------

def _parse_matrix(raw: Any, name: str = "matrix") -> list[list[Fraction]]:
    if not isinstance(raw, list) or not raw or not all(isinstance(row, list) and row for row in raw):
        raise MathEvalError(f"{name} must be a non-empty 2D numeric list")
    rows = len(raw)
    cols = len(raw[0])
    if rows > MAX_MATRIX_DIM or cols > MAX_MATRIX_DIM:
        raise MathEvalError(f"{name} exceeds max dimension {MAX_MATRIX_DIM}x{MAX_MATRIX_DIM}")
    if any(len(r) != cols for r in raw):
        raise MathEvalError(f"{name} rows must all have equal length")
    return [[_as_fraction(v) for v in row] for row in raw]


def _matrix_rref(mat: list[list[Fraction]]) -> tuple[list[list[Fraction]], int, Fraction]:
    m = [list(r) for r in mat]
    rows, cols = len(m), len(m[0])
    r = 0
    det = Fraction(1)
    for c in range(cols):
        if r >= rows:
            break
        piv = None
        for i in range(r, rows):
            if m[i][c] != 0:
                piv = i
                break
        if piv is None:
            det = Fraction(0)
            continue
        if piv != r:
            m[r], m[piv] = m[piv], m[r]
            det = -det
        pv = m[r][c]
        det *= pv
        m[r] = [v / pv for v in m[r]]
        for i in range(rows):
            if i != r and m[i][c] != 0:
                f = m[i][c]
                m[i] = [m[i][j] - f * m[r][j] for j in range(cols)]
        r += 1
    if rows != cols or r < rows:
        det = Fraction(0)
    return m, r, det


def _matrix_op(op: str, a_raw: Any, b_raw: Any = None) -> dict[str, Any]:
    op_lc = op.strip().lower()
    if op_lc in ("dot", "cross", "norm"):
        if not isinstance(a_raw, list) or not a_raw:
            raise MathEvalError(f"{op_lc} requires a non-empty numeric vector")
        va = [_as_fraction(v) for v in a_raw]
        if op_lc == "norm":
            sq = sum(v * v for v in va)
            return {"ok": True, "op": "norm", "exact_squared": _format_value(sq), "result": math.sqrt(float(sq))}
        if not isinstance(b_raw, list) or len(b_raw) != len(va):
            raise MathEvalError(f"{op_lc} requires two equal-length vectors")
        vb = [_as_fraction(v) for v in b_raw]
        if op_lc == "dot":
            return {"ok": True, "op": "dot", "result": _format_value(sum(x * y for x, y in zip(va, vb)))}
        if len(va) != 3:
            raise MathEvalError("cross product requires 3D vectors")
        res = [
            va[1] * vb[2] - va[2] * vb[1],
            va[2] * vb[0] - va[0] * vb[2],
            va[0] * vb[1] - va[1] * vb[0],
        ]
        return {"ok": True, "op": "cross", "result": [_format_value(x) for x in res]}

    a = _parse_matrix(a_raw, "matrix_a")
    rows, cols = len(a), len(a[0])
    if op_lc == "transpose":
        t = [[a[r][c] for r in range(rows)] for c in range(cols)]
        return {"ok": True, "op": "transpose", "result": _format_value(t)}
    if op_lc in ("det", "rank", "rref"):
        rref_m, rank, det = _matrix_rref(a)
        if op_lc == "det":
            if rows != cols:
                raise MathEvalError("determinant requires a square matrix")
            return {"ok": True, "op": "det", "result": _format_value(det)}
        if op_lc == "rank":
            return {"ok": True, "op": "rank", "result": rank}
        return {"ok": True, "op": "rref", "rank": rank, "result": _format_value(rref_m)}
    if op_lc == "inv":
        if rows != cols:
            raise MathEvalError("matrix inverse requires a square matrix")
        aug = [a[i] + [Fraction(1 if i == j else 0) for j in range(rows)] for i in range(rows)]
        rref_m, rank, _ = _matrix_rref(aug)
        if rank < rows:
            raise MathEvalError("matrix is singular (not invertible)")
        inv_m = [row[rows:] for row in rref_m]
        return {"ok": True, "op": "inv", "result": _format_value(inv_m)}
    if op_lc == "matmul":
        b = _parse_matrix(b_raw, "matrix_b")
        if cols != len(b):
            raise MathEvalError(f"matmul shape mismatch: {rows}x{cols} vs {len(b)}x{len(b[0])}")
        b_cols = len(b[0])
        prod = [
            [sum(a[i][k] * b[k][j] for k in range(cols)) for j in range(b_cols)]
            for i in range(rows)
        ]
        return {"ok": True, "op": "matmul", "result": _format_value(prod)}
    raise MathEvalError(f"unsupported matrix op: {op!r} (use det, inv, rref, rank, transpose, matmul, dot, cross, norm)")


# --- Unit conversion & date arithmetic ---------------------------------------

_UNIT_GROUPS: dict[str, dict[str, float]] = {
    "length": {
        "m": 1.0, "meter": 1.0, "meters": 1.0, "km": 1_000.0, "cm": 0.01, "mm": 0.001,
        "um": 1e-6, "nm": 1e-9, "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
        "ft": 0.3048, "foot": 0.3048, "feet": 0.3048, "yd": 0.9144, "yard": 0.9144,
        "mi": 1609.344, "mile": 1609.344, "miles": 1609.344, "nmi": 1852.0,
    },
    "mass": {
        "kg": 1.0, "g": 0.001, "mg": 1e-6, "t": 1_000.0, "tonne": 1_000.0,
        "lb": 0.45359237, "lbs": 0.45359237, "oz": 0.028349523125,
    },
    "time": {
        "s": 1.0, "sec": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9,
        "min": 60.0, "h": 3600.0, "hr": 3600.0, "hour": 3600.0,
        "d": 86400.0, "day": 86400.0, "days": 86400.0, "wk": 604800.0, "week": 604800.0,
        "yr": 31557600.0, "year": 31557600.0,
    },
    "data": {
        "b": 1.0, "bit": 1.0, "bits": 1.0, "B": 8.0, "byte": 8.0, "bytes": 8.0,
        "KB": 8_000.0, "MB": 8_000_000.0, "GB": 8_000_000_000.0, "TB": 8_000_000_000_000.0,
        "KiB": 8.0 * 1024, "MiB": 8.0 * 1024**2, "GiB": 8.0 * 1024**3, "TiB": 8.0 * 1024**4,
        "kb": 1_000.0, "Mb": 1_000_000.0, "Gb": 1_000_000_000.0,
        "Kib": 1024.0, "Mib": 1024.0**2, "Gib": 1024.0**3,
    },
    "speed": {
        "m/s": 1.0, "km/h": 1.0 / 3.6, "kph": 1.0 / 3.6, "mph": 0.44704, "knot": 1852.0 / 3600.0, "knots": 1852.0 / 3600.0,
    },
    "angle": {
        "rad": 1.0, "deg": math.pi / 180.0, "grad": math.pi / 200.0, "turn": 2.0 * math.pi,
    },
    "energy": {
        "J": 1.0, "kJ": 1_000.0, "MJ": 1_000_000.0, "Wh": 3600.0, "kWh": 3_600_000.0,
        "cal": 4.184, "kcal": 4184.0, "BTU": 1055.05585262, "eV": 1.602176634e-19,
    },
    "volume": {
        "L": 1.0, "l": 1.0, "mL": 0.001, "ml": 0.001, "m3": 1000.0,
        "gal": 3.785411784, "qt": 0.946352946, "pt": 0.473176473, "cup": 0.2365882365, "floz": 0.0295735295625,
    },
}


def _convert_units(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    fu = from_unit.strip()
    tu = to_unit.strip()
    # Temperature special case
    temp_map = {"c": "C", "celsius": "C", "f": "F", "fahrenheit": "F", "k": "K", "kelvin": "K"}
    if fu.lower() in temp_map and tu.lower() in temp_map:
        f_k = temp_map[fu.lower()]
        t_k = temp_map[tu.lower()]
        if f_k == "C":
            kelvin = value + 273.15
        elif f_k == "F":
            kelvin = (value - 32.0) * 5.0 / 9.0 + 273.15
        else:
            kelvin = value
        if t_k == "C":
            out = kelvin - 273.15
        elif t_k == "F":
            out = (kelvin - 273.15) * 9.0 / 5.0 + 32.0
        else:
            out = kelvin
        return {"ok": True, "category": "temperature", "input": value, "from": f_k, "to": t_k, "result": out}

    for category, table in _UNIT_GROUPS.items():
        # Exact match first (preserves b vs B, mL vs ML), then case-insensitive fallback
        f_factor = table.get(fu)
        t_factor = table.get(tu)
        if f_factor is None or t_factor is None:
            lower_table = {k.lower(): v for k, v in table.items()}
            if category != "data":
                f_factor = f_factor if f_factor is not None else lower_table.get(fu.lower())
                t_factor = t_factor if t_factor is not None else lower_table.get(tu.lower())
        if f_factor is not None and t_factor is not None:
            out = value * (f_factor / t_factor)
            return {"ok": True, "category": category, "input": value, "from": fu, "to": tu, "result": out}
    raise MathEvalError(f"incompatible or unknown units: {fu!r} -> {tu!r}")


def _date_calc(date_a: str, date_b: str = "", add_days: int | None = None) -> dict[str, Any]:
    def _parse(s: str) -> _dt.datetime:
        text = s.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            return _dt.datetime.fromtimestamp(float(text), tz=_dt.timezone.utc)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(text)

    dt_a = _parse(date_a)
    if add_days is not None:
        shifted = dt_a + _dt.timedelta(days=int(add_days))
        return {
            "ok": True,
            "input": dt_a.isoformat(),
            "add_days": int(add_days),
            "result": shifted.isoformat(),
            "weekday": shifted.strftime("%A"),
            "epoch_seconds": shifted.timestamp() if shifted.tzinfo else None,
        }
    if date_b:
        dt_b = _parse(date_b)
        if (dt_a.tzinfo is None) != (dt_b.tzinfo is None):
            dt_a = dt_a.replace(tzinfo=_dt.timezone.utc) if dt_a.tzinfo is None else dt_a
            dt_b = dt_b.replace(tzinfo=_dt.timezone.utc) if dt_b.tzinfo is None else dt_b
        delta = dt_b - dt_a
        return {
            "ok": True,
            "from": dt_a.isoformat(),
            "to": dt_b.isoformat(),
            "days": delta.days,
            "total_seconds": delta.total_seconds(),
            "total_hours": delta.total_seconds() / 3600.0,
            "weeks": delta.total_seconds() / 604800.0,
        }
    return {
        "ok": True,
        "iso": dt_a.isoformat(),
        "weekday": dt_a.strftime("%A"),
        "day_of_year": dt_a.timetuple().tm_yday,
        "iso_week": dt_a.isocalendar().week,
    }


def _numerical_derivative(expr: str, var: str, point: float, variables: dict[str, Any]) -> dict[str, Any]:
    h = 1e-6 * max(1.0, abs(point))
    f_plus = _eval_fn_at(expr, var, point + h, variables)
    f_minus = _eval_fn_at(expr, var, point - h, variables)
    deriv = (f_plus - f_minus) / (2.0 * h)
    f0 = _eval_fn_at(expr, var, point, variables)
    second = (f_plus - 2.0 * f0 + f_minus) / (h * h)
    return {"ok": True, "kind": "derivative", "variable": var, "at": point, "first": deriv, "second": second}


def _numerical_integral(expr: str, var: str, low: float, high: float, variables: dict[str, Any], steps: int = 400) -> dict[str, Any]:
    n = max(4, min(MAX_SOLVER_STEPS, int(steps)))
    if n % 2 == 1:
        n += 1
    h = (high - low) / n
    total = _eval_fn_at(expr, var, low, variables) + _eval_fn_at(expr, var, high, variables)
    for i in range(1, n):
        weight = 4.0 if i % 2 == 1 else 2.0
        total += weight * _eval_fn_at(expr, var, low + i * h, variables)
    return {"ok": True, "kind": "integral", "variable": var, "bounds": [low, high], "result": total * h / 3.0, "steps": n}


def math_eval(
    expression: str = "",
    *,
    action: str = "eval",
    exact: bool = False,
    precision: int = 0,
    variables: dict[str, Any] | None = None,
    variable: str = "x",
    equations: list[str] | None = None,
    bracket: list[float] | None = None,
    matrix_op: str = "",
    matrix_a: Any = None,
    matrix_b: Any = None,
    value: float | None = None,
    from_unit: str = "",
    to_unit: str = "",
    date_a: str = "",
    date_b: str = "",
    add_days: int | None = None,
    at: float = 0.0,
    low: float = 0.0,
    high: float = 1.0,
) -> dict[str, Any]:
    """Evaluate exact/high-precision math, equations, matrices, statistics, units, or dates."""
    act = str(action or "eval").strip().lower() or "eval"
    vars_dict = dict(variables or {})

    try:
        if act == "eval":
            expr = _preprocess_expression(expression)
            tree = ast.parse(expr, mode="eval")
            _check_ast_bounds(tree)
            if precision and int(precision) > 0:
                prec = max(MIN_PRECISION, min(MAX_PRECISION, int(precision)))
                with localcontext() as ctx:
                    ctx.prec = prec
                    dec_env = {k: Decimal(str(v)) for k, v in vars_dict.items()}
                    val = _eval_decimal_ast(tree, dec_env)
                return {"ok": True, "action": "eval", "precision": prec, "result": _format_value(val)}
            funcs = _build_safe_funcs(exact=bool(exact))
            val = _eval_ast(tree, vars_dict, funcs, exact=bool(exact))
            return {"ok": True, "action": "eval", "exact": bool(exact), "result": _format_value(val)}

        if act == "solve":
            if equations:
                var_list = [v.strip() for v in str(variable or "").split(",") if v.strip()]
                if not var_list and isinstance(variables, dict):
                    var_list = list(variables.keys())
                return _solve_linear_system(list(equations), var_list)
            br = (float(bracket[0]), float(bracket[1])) if bracket and len(bracket) == 2 else None
            return _solve_1d_equation(expression, variable=variable or "x", bracket=br, variables=vars_dict)

        if act == "matrix":
            return _matrix_op(matrix_op or expression, matrix_a, matrix_b)

        if act == "convert":
            val_num = float(value) if value is not None else float(_eval_ast(ast.parse(_preprocess_expression(expression), mode="eval"), vars_dict, _build_safe_funcs(False)))
            return _convert_units(val_num, from_unit, to_unit)

        if act == "date":
            return _date_calc(date_a or expression, date_b=date_b, add_days=add_days)

        if act == "derivative":
            return _numerical_derivative(_preprocess_expression(expression), variable or "x", float(at), vars_dict)

        if act == "integrate":
            return _numerical_integral(_preprocess_expression(expression), variable or "x", float(low), float(high), vars_dict)

        raise MathEvalError(
            f"unknown action {act!r}; expected eval, solve, matrix, convert, date, derivative, or integrate"
        )
    except (MathEvalError, SyntaxError, InvalidOperation, ValueError, ZeroDivisionError) as exc:
        return {"ok": False, "action": act, "error": str(exc)}


__all__ = ["MathEvalError", "math_eval"]
