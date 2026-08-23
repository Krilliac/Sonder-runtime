# Adoption experiment template

Status: **process template** (docs-only; imposes no runtime behavior).
Scope: one borrowed mechanism from an external harness, evaluated against
Sonder's existing contracts before any production change is proposed.

Copy this file to `docs/research/experiments/EXP-<nnn>-<slug>.md` and fill in
every section. An experiment that cannot fill in **Grounding** or **No-go
constraints** is not ready to run.

---

## EXP-<nnn>: <one-line mechanism name>

- **Source harness:** <project> — <exact doc/code reference (URL or path)>
- **Borrowed mechanism:** <one paragraph, in Sonder vocabulary, of what the
  mechanism does in its home project. No aspiration, only what that project
  ships.>
- **Status:** proposed | running | accepted | rejected
- **Label:** experimental until a promotion decision is recorded below.

### Grounding in Sonder

| Question | Answer |
|---|---|
| Which Sonder module(s) would own it? | e.g. `master_orchestrator.py`, `fleet_provenance.py` |
| What existing mechanism does it extend or replace? | cite file / ADR |
| Which trust boundary does it sit on? | permission modes, HTTP roles, selfmod protected paths, none |
| Does it move any data off-host? | must be "no" or name the explicit consent gate |

### Hypothesis

One falsifiable sentence: "Adding <X> to <module> will <measurable effect>
without <protected property> regressing."

### Method

- Smallest possible seam: prefer a diagnostics surface, a shadow computation,
  or an offline replay over a behavior change.
- Exact commands to run, and the dataset/fixture used (checked into
  `docs/research/experiments/` or `tests/` if small; referenced by path and
  hash otherwise).

### Acceptance criteria

Numbers decided **before** the run. At minimum:

1. A RED proof: show the measurement failing (or absent) before the change.
   A check that cannot fail is not a check.
2. The target metric with its threshold.
3. The guard metric(s) that must not regress (latency, memory, test count —
   record the exact pre-change values).

### No-go constraints

Constraints that terminate the experiment regardless of results. Defaults for
every experiment (extend, never trim):

- No new network egress without an existing consent gate
  (`SONDER_ALLOW_CLOUD`, `SONDER_ALLOW_REMOTE_OLLAMA`).
- No writes to selfmod-protected control-plane files (`selfmod.protected_paths()`).
- No new third-party runtime dependency without a separate dependency decision.
- No weakening of a deny-by-default path (`tool_contract.SYSTEM_OPERATION_UNBOUND`,
  `permission_modes` risk classes) to make the experiment pass.
- No mutable-alias deployment identities (ADR-005).

### Result and promotion decision

- Measured values vs. acceptance criteria (paste real output, not summaries).
- Decision: promote (link the follow-up change/PR), iterate, or reject.
- If rejected: one sentence on why, so the landscape doc can record it and the
  experiment is not silently re-proposed.
