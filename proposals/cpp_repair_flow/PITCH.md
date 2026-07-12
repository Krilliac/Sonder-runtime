# cpp_repair_flow

`solve_cpp(prompt, gen_fn, max_attempts=3, spec=None, extract_fn=None, verify_fn=None)` is a
one-line C++ entry point over the existing `solver.solve_verified()` loop: it pins the verifier
to the already-registered `'cpp_compile'` backend (MSVC `/c` via vcvars) and forwards everything
else, so callers get "generate C++ -> compile -> repair on the compiler error" without having to
know the registry key or hand-thread `spec`/`verify_fn` through `solve_verified`'s full signature.

It's valuable because C++ is Sonder Runtime's primary domain (mangos/Cambrian/MMORPG/SparkEngine are
all MSVC C++), yet today reaching compile-grounded self-repair requires remembering to say
`solver.solve_verified(..., verifier="cpp_compile")` by hand each time — a one-liner removes that
friction and makes compile-repair the obvious default for any C++ generation call site.

Zero existing files are touched: `verifiers.cpp_compile` and `solver.solve_verified` are imported
read-only. `test_cpp_repair_flow.py` proves a broken -> fixed convergence, a clean give-up after
`max_attempts`, missing-code-block recovery, and correct registry wiring — all via a stubbed
"compiler" (`verify_fn`) and canned `gen_fn` responses, so the suite needs no real MSVC/vcvars.

To integrate: `from proposals.cpp_repair_flow import cpp_repair_flow as crf` (or copy the module
in), then call `crf.solve_cpp(task_prompt, real_gen_fn, spec={"std": "c++17"})` anywhere C++ is
generated — `server.py`'s C++ code paths, `curriculum_run.py`'s C++ tasks, or `eval_solver.py`'s
per-verifier benchmarking, all of which already know how to drive `solve_verified`-shaped results.
