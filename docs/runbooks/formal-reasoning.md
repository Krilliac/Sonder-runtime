# Formal reasoning setup

Sonder's formal path separates model output from proof evidence: a model writes
Lean 4 source, `lean_check` rejects explicit proof placeholders, and Lean's
kernel decides whether the artifact checks. A successful numerical experiment
or persuasive explanation is not a substitute for that verdict.

## Install and pin the toolchain

Install Lean through the official `elan` toolchain manager, then install the
release named by the repository's `lean-toolchain` file. Keep the installation
outside the source checkout. Confirm both fixed, non-interactive probes:

```bash
lean --version
lake --version
```

For core-language theorems, configure only Lean:

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
example (a b : ℝ) : (a + b) ^ 2 = a ^ 2 + 2 * a * b + b ^ 2 := by
  ring
"""
print(verifiers.lean_check(source))
PY
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
