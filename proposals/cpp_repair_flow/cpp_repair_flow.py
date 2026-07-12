"""cpp_repair_flow — a thin C++-specific seam over solver.solve_verified().

solver.solve_verified() already generalizes sonder's generate->verify->repair
loop to ANY named backend in verifiers.REGISTRY: it takes a `verifier` key, calls
verifiers.verify(verifier, code, spec) (or an injected verify_fn), and feeds the
verifier's Verdict.detail back into the repair prompt on failure. cpp_compile is
already registered (MSVC /c compile-only via vcvars) — nothing in solver.py or
verifiers.py needs to change to get C++ self-repair working.

What's missing is a one-line entry point: callers who want "generate C++, compile
it, repair on the compiler error" currently have to know the registry key
('cpp_compile'), remember solve_verified's full signature, and thread spec/
verify_fn through by hand. solve_cpp() is that entry point — it fixes the
verifier name and forwards everything else, so integration is a single import
and a single call.

gen_fn and verify_fn are dependency-injected (same contract as solve_verified),
so this module's tests exercise a full broken->fixed convergence with a stub
"compiler" and no real MSVC/vcvars on the box.
"""
import solver

CPP_VERIFIER = "cpp_compile"


def solve_cpp(prompt, gen_fn, max_attempts=3, spec=None, extract_fn=None, verify_fn=None):
    """Generate C++ for `prompt`, compile it via the 'cpp_compile' verifier, and
    repair on compiler error. Thin wrapper: all the looping/repair-prompt logic
    lives in solver.solve_verified(); this function only pins the verifier name.

    prompt      — the C++ task description. Fed to gen_fn and echoed into the
                  repair prompt alongside the failing code + compiler output.
    gen_fn(prompt) -> str containing a fenced code block with candidate C++
                  source (see grounding.extract_code_block: a bare ``` or
                  ```python fence; a ```cpp fence is NOT recognized).
    max_attempts — generate/verify/repair rounds before giving up.
    spec        — forwarded to verifiers.cpp_compile, e.g. {'std': 'c++17',
                  'vcvars': path, 'timeout': secs}. Ignored if verify_fn is
                  given (a supplied verify_fn owns its own judging logic).
    extract_fn  — overrides grounding.extract_code_block; None keeps the
                  solve_verified default.
    verify_fn   — overrides the real compiler call; a fn(code) -> Verdict.
                  Tests MUST inject a stub here (or monkeypatch
                  verifiers.verify) so the suite needs no real MSVC install.

    Returns solve_verified's {passed, code, attempts, transcript} dict.
    """
    kwargs = {"spec": spec, "max_attempts": max_attempts, "verify_fn": verify_fn}
    if extract_fn is not None:
        kwargs["extract_fn"] = extract_fn
    return solver.solve_verified(prompt, gen_fn, CPP_VERIFIER, **kwargs)
