# Formal reasoning setup

Sonder's formal path separates model output from proof evidence: a model writes
Lean 4 source, `lean_check` rejects `sorry`, `admit`, `sorryAx`, `axiom`, and
`constant` trust gaps, and Lean's kernel decides whether the artifact checks.
For a task-bound check, Sonder then loads the compiled module in a separate
trusted audit and rejects any transitive axiom dependency introduced by the
submitted artifact, including axioms added through metaprogramming. Imported
axioms come only from the configured pinned toolchain/project. A successful
numerical experiment or persuasive explanation is not a substitute for that
verdict.

Raw `lean_check(source)` is a compiler check. Task completion must also supply
`expected_declaration` and `expected_type`; the verifier compiles that type in a
separate trusted module after the artifact compiler process exits, then compares
the two kernel types in its trusted audit. Submitted syntax and macros therefore
cannot rewrite the contract check or discover the randomized contract module
during compilation. `solve_lean` requires this contract, so a valid proof of an
unrelated theorem cannot satisfy the request. The same audit checks that
declaration's transitive axiom dependencies, including an attempted dependency
on the trusted contract witness itself.

If the contract type needs task-local predicates, structures, or functions,
put those definitions in `trusted_prelude`. Sonder compiles that caller-owned
source into a separate module and imports it into both the contract and proof
modules; the proof never imports the module containing the contract axiom.
Only task authors may populate this field—never copy model output into it. The
generation loop shows the prelude to the model and tells it to use, not repeat,
those declarations.

## Install and pin the toolchain

Install Lean through the official `elan` toolchain manager, then install the
release named by the repository's `lean-toolchain` file. Keep the installation
outside the source checkout. Confirm both fixed, non-interactive probes:

```bash
lean --version
lake --version
```

For core-language theorems, the default `lean` command is run from an isolated
directory containing the repository's `lean-toolchain` pin, and its reported
version must match that pin. An explicit executable remains available for
provisioned deployments:

```bash
export SONDER_LEAN_EXE=/absolute/path/to/lean
```

## Enable Mathlib

Create a separate Lake project whose `lean-toolchain` and Mathlib revision are
both pinned. Fetch its dependencies and precompiled cache during provisioning,
not during a proof check. Then configure all three paths:

```bash
export SONDER_LEAN_EXE=/absolute/path/to/lean
export SONDER_LAKE_EXE=/absolute/path/to/lake
export SONDER_LEAN_PROJECT=/absolute/path/to/pinned-mathlib-project
```

The project must contain `lakefile.toml` or `lakefile.lean`. With this setting,
`lean_check` runs `lake env lean` from that project, so an artifact may begin
with `import Mathlib` while retaining the project's locked dependency graph.

## Smoke check

From the Sonder source checkout:

```bash
python - <<'PY'
import verifiers

source = """import Mathlib
theorem square_sum (a b : ℝ) : (a + b) ^ 2 = a ^ 2 + 2 * a * b + b ^ 2 := by
  ring
"""
print(verifiers.lean_check(source, {
    "expected_declaration": "square_sum",
    "expected_type": "∀ (a b : ℝ), (a + b) ^ 2 = a ^ 2 + 2 * a * b + b ^ 2",
}))
PY
```

For a contract with a task-local definition:

```python
source = "theorem requested : IsZero 0 := rfl\n"
print(verifiers.lean_check(source, {
    "expected_declaration": "requested",
    "expected_type": "IsZero 0",
    "trusted_prelude": "def IsZero (n : Nat) : Prop := n = 0\n",
}))
```

A missing executable or invalid project raises `VerifierUnavailable`. A source
or kernel error returns a failed verdict with bounded diagnostics. Only
`Verdict(passed=True, reason='checked', ...)` is machine-check evidence.

## Model generation policy

When a local model is used to draft a small Lean artifact, pass
`options={"think": False, ...}` through `ModelRequest`. This spends the bounded
output allowance on the fenced source instead of allowing a native-thinking
model to consume it before emitting code. The normal `solve_lean` loop still
feeds a failed kernel diagnostic into the next bounded repair attempt.

For open-ended mathematical investigation, leave thinking enabled on the
local `reasoning` tier. If one reasoning segment reaches `num_predict` without
an answer, Sonder carries one compact private checkpoint into the next segment
under a hard aggregate token limit and the same deadline. This is distinct
from conversation compaction, which makes room in the model's input context.
Replacement matches only the exact last checkpoint message created by Sonder;
caller messages are preserved even when their text begins with the public
checkpoint marker.
