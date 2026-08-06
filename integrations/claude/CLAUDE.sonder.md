# Sonder Runtime — agent guidance

Paste this into your `~/.claude/CLAUDE.md` (or a project `CLAUDE.md`) once the
MCP server is registered. It is written to be dropped in verbatim; nothing in
it is specific to one machine.

---

## Sonder Runtime (local model offload)

The `sonder` MCP server runs a local model on your own GPU through Ollama.
Use it to spend a small, private, fast model instead of your own context
budget — but only on the kind of work it is actually good at.

### What to send it: transformation, not recall

This is the whole rule, and it is not the same as "mechanical vs hard".
Measured over seven judged offloads to a 7B `tier=code`:

- **Transformation — every fact it needs is already in your prompt.**
  Restructure this enum into a switch. Mirror this struct for the other
  bitness. Turn these bullet facts into prose. Implement this fully specified
  function. **4/4 usable.** Send it as much of this as you have.

- **Recall — it must supply a fact you did not give it. 3/3 wrong, and
  confidently.** Asked for the Win32 ROP2 truth table it returned **10 of 16
  entries wrong**, including inverting the wrong operand. Asked for HID usage
  codes it gave two different devices the same number.
  **A lookup table looks maximally mechanical and is the worst case, because
  it is pure recall.** If you cannot paste the facts, do not offload it: you
  would be asking a small model to remember, its weakest axis, and you would
  have to verify every row anyway — at which point you have done the work.

- **There is a complexity ceiling inside transformation too.** A fully
  specified escape-sequence parser — parameter defaults conditioned on the
  final byte, incomplete-input handling — came back with an out-of-bounds
  read. The failure mode is not a visibly wrong answer; it is plausible
  looking, memory-unsafe code. Keep an offloaded spec to one job with few
  interacting rules.

Calibration: `learning_health_status` reports two hit rates. On self-generated
curriculum, where the runtime sets and marks its own exam, it scores in the
high nineties. On work a caller delegated and then judged, expect roughly
**half**. Budget for reviewing all of it and rejecting a lot of it.

### Always

- **Audit before applying.** Treat it as a junior implementer: useful,
  private, fast, and fallible. Never paste its output into a build unread.
- **Never offload correctness-critical logic**: locking, memory management,
  crypto, hot paths, anything where a subtle wrong answer is worse than no
  answer.
- **Keep private code on local tiers.** `fast`, `code`, and `general` run on
  your machine. `cloud-code` and `cloud-general` leave it — they are metered
  and are an explicit opt-in per call.
- **Record the outcome.** A learning-tier reply ends with
  `[interaction_id: <id>]`. Pass it to `record_outcome(interaction_id, signal)`
  once you know whether it compiled or passed — `tests_passed`, `accepted`,
  `edited`, `rejected`, `failed`. This is the only thing that makes the loop
  learn. Negative signals are the scarce ones and matter most; a store that is
  96% positive has little to learn from.

### Subagents and fleets

Subagents do **not** offload on their own. If you want a fan-out to use
Sonder, each agent's prompt must say so explicitly — tell it to run
`ToolSearch "select:mcp__sonder__offload"` first, name the mechanical bulk you
want offloaded, and require it to review and fix the output before use.

Offloading saves *token budget*, not wall-clock, whenever the job is dominated
by compiles or agent iteration rather than generation. Do not assume more
offload means faster.

### Useful tools beyond `offload`

| Tool | Use |
|---|---|
| `status`, `diagnostics` | Check tiers, VRAM residency, and health before a batch |
| `memory_search` | Retrieve previously distilled lessons |
| `record_outcome` | Close the learning loop (see above) |
| `run_code` | Bounded execution in ~15 languages |
| `learning_health_status` | Judged vs self-graded hit rates, memory hygiene |
| `npu_status` | NPU accelerator state, if present |

Full surface: `tool_manifest`.
