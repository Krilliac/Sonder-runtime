# Formal reasoning setup

Sonder's formal path separates model output from proof evidence: a model writes
Lean 4 source, `lean_check` rejects `sorry`, `admit`, `sorryAx`, `axiom`, and
`constant` trust gaps, and Lean's kernel decides whether the artifact checks.
The model-authored source is compiled only in a guarded local Linux OCI
container; it is never passed to a host Lean process.
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

## Configure the containment image

Formal verification is fail-closed until an operator provides a locally
installed, pinned OCI image containing the approved Lean toolchain and any
approved libraries. The image must expose `lean` (or the explicitly configured
image-local executable) and `/usr/bin/env`. It is inspected to an immutable
image ID before each run; the verifier never pulls an image at request time.

Set all of the following in the runtime service environment:

```text
SONDER_ISOLATED_RUNTIME=docker                 # or podman; a ready local Linux engine
SONDER_ISOLATED_ROOTS=/absolute/scratch-parent
SONDER_LEAN_SANDBOX_ROOT=/absolute/scratch-parent
SONDER_LEAN_SANDBOX_IMAGE=registry/lean@sha256:<pinned-image-digest>
SONDER_LEAN_SANDBOX_EXECUTABLE=lean            # optional image-local path/name
```

The scratch parent must already exist, be inside `SONDER_ISOLATED_ROOTS`, and
contain no links, sockets, devices, or secrets. Each check creates an empty
temporary child below that parent. The guarded executor mounts only that child,
with network disabled, a read-only root filesystem, no Linux capabilities,
`no-new-privileges`, UID/GID 65534, a bounded writable scratch mount, and fixed
CPU, memory, PID, timeout, and output ceilings. It uses no host project mount,
Docker socket mount, device passthrough, or inherited runtime environment.

Host `SONDER_LEAN_EXE`, `SONDER_LAKE_EXE`, and `SONDER_LEAN_PROJECT` values are
not executable authority for this path. A container image owns its complete
toolchain and dependencies; host Lake projects are rejected rather than mounted
into the container. Build and admit a separate, digest-pinned image for Mathlib
or another approved dependency set during provisioning.

## Install and pin the toolchain image

Build the image from the release named by the repository's `lean-toolchain`
file, with all dependencies preinstalled. Keep source and toolchain provisioning
outside the live runtime. Confirm the image's fixed, non-interactive probes:

```bash
lean --version
lake --version
```

For core-language theorems, the image-local `lean` command is run from the
guarded temporary directory. Without legacy host executable configuration, its
reported version must match the repository pin.

## Enable Mathlib

Create a separate image whose Lean toolchain and Mathlib revision are both
pinned. Fetch dependencies and precompiled cache during image provisioning, not
during a proof check. Point `SONDER_LEAN_SANDBOX_IMAGE` at that immutable image
digest before verifying an artifact that begins with `import Mathlib`.

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
