# Production capability evidence routing

Implementation notes for Issue #510 Series E, 2026-09-29. These are review notes
for an uncommitted change; they do not claim deployment or live-model qualification.

## Eligibility policy

Production opens `<state home>/capability_evidence.json` at bootstrap. The file is
read on admission, so a completed refresh is visible without restarting. The
Ollama pool's production factory also opens that store; both single-host sends
and multi-worker selection check the actual request payload.

Production defaults to **advisory**. Requests with tools, structured output
(`format`/`response_format`), images or long inputs remain routable without a
refresh. A candidate is excluded only when a required capability has a recent
**measured failure** (`passed: false`) bound to the current host/model identity.
Missing, corrupt, unknown (`passed: null`), synthetic, future-dated, stale or
identity-mismatched evidence is **unverified**, not a refusal or a measured pass.
Tier reasons and gateway/pool logs report that distinction.

Select the mode through the compatibility environment flag before startup:

```powershell
$env:SONDER_CAPABILITY_ROUTING = "advisory" # default; only measured failures exclude
$env:SONDER_CAPABILITY_ROUTING = "strict"   # opt in to requiring measured passes
$env:SONDER_CAPABILITY_ROUTING = "off"      # ignore capability evidence entirely
```

The flag is read at the adapter boundary and passed explicitly to application
policy. An unset or empty value selects advisory; invalid values raise a
configuration error. This change uses the environment option, not a new TOML
section. Restart to change the mode of a composed gateway/worker pool.

| Mode | Missing/unverified evidence | Current measured failure | Every candidate fails |
| --- | --- | --- | --- |
| `advisory` | Eligible, reason says `unverified` | Prefer another eligible candidate | Retain configured routing; signal `fallback_used` |
| `strict` | Ineligible | Ineligible | Refuse (`tier=None` / capability-unavailable error) |
| `off` | Ignored | Ignored | Existing routing, without evidence/identity checks |

In advisory mode, a measured failure must not strand a request. If all candidate
tiers or otherwise admissible workers fail the evidence check, selection retains
the existing configured route/worker policy. Tier results set `fallback_used=true`;
gateway and pool fallback logs include `fallback_used` and the measured-failure
reason. The single-route gateway and primary-only pool path likewise dispatch
the configured route if it is the only choice. Existing membership, transport,
model-inventory, consent, capacity and circuit rules still apply. No cloud or
remote permission is added. Transport errors still surface normally.

Plain text and embeddings retain their existing policies. In particular, plain
text remains eligible without evidence even if a stored chat probe failed.
Lexical/semantic classification is unchanged; evidence reasons survive semantic
selection. Dependency-injected routers/pools without a store retain their legacy
behavior; production always supplies a store.

Tools plus a format still add the combined `tools_with_schema` requirement.
In advisory mode an absent/unknown combined probe permits an unverified request;
a measured `tools_with_schema=false` excludes that candidate (subject to the
all-failed fallback). Individual requested tool/schema failures also exclude.
Strict mode requires passing tool, schema **and** combined probes; passing only
the two individual probes is insufficient. The combined probe itself is unchanged.

The long-input trigger is 8192 declared approximate tokens or 24,576 UTF-8 bytes
across prompt, system and messages. It is a conservative routing estimate, not
token accounting. An allocated `num_ctx` alone does not make short chat a long
input. Callers may explicitly require long context as well.

Evidence expires after 24 hours. An observed different model digest, Ollama version,
template, context configuration or host/origin binding invalidates it.
A stale failure or a failure for the old identity is ignored in advisory mode, so
an updated model is eligible again as unverified. Missing identity is also
unverified. Remote workers cannot inherit local measured evidence; advisory can
still use them under their existing consent and membership rules.

The request gateway does no identity observation in advisory mode unless the
store contains a fresh, nonsynthetic, identity-bound **failure of a requested
capability** for the route's model. Empty stores, passes, unknowns and unrelated
failures therefore cause **zero identity calls**, including after dispatch.

When needed, the gateway caches observations (including unavailable results) for
60 seconds per origin, model and requested context window. Production supplies
the key from the same adapter configuration used by identity discovery. Cache
entries also bind to the evidence file revision: an atomic refresh write,
including a failed refresh or a write from a separate CLI process, invalidates
them on the next lookup. This does not cache capability verdicts or extend the
24-hour evidence lifetime.

Strict mode retains its post-dispatch identity comparison, using the same cache.
Ordinary consecutive requests cost one observation on the first cache miss and
zero thereafter within the TTL. A request spanning TTL expiry or a refresh can
observe again after dispatch. The trade-off is that an unrefreshed backend change
may remain undetected for up to 60 seconds; the post-dispatch check is no longer
an unconditional fresh probe. Advisory has no post-dispatch observation.

A different returned model logs a warning and returns the response in advisory
mode; strict mode raises. Ollama names are compared with the probe's existing
implicit `:latest` normalization, now shared through the domain layer. Other
providers retain exact-name comparison.
No mode retries a completed response for an evidence or identity error. The separate explicit strict
role-routing API remains strict, including semantic, embedding and budget
requirements; the production request-mode flag does not weaken that API.

## Refresh on the host

Run from an installed/current runtime with its normal state and Ollama settings:

```powershell
python -m sonder_runtime capabilities refresh --json
```

Use `--model <configured-model>` repeatedly to refresh a subset, `--timeout 120`
for the per-model wall budget (maximum 300 seconds), and `--context-tokens 8192`
to pin the probe `num_ctx`. The default follows the runtime context policy; the
context must match the requests being admitted. `--config <sonder.toml>` and
`--set state.home=<path>` use the usual CLI configuration. The command reads the
canonical runtime policy's local model bindings, including `SONDER_RUNTIME_POLICY`.
It runs at most sixteen unique models serially. It never pulls models or sends
cloud requests. The endpoint must be an explicit loopback HTTP origin.

The battery uses fixed public prompts, an in-process allowlisted echo function,
and the existing protocol-trace validator. It measures chat, JSON-schema output,
native tools, tools with schema, sequential tools, and continuation. The combined
probe sends both `tools` and JSON-schema `format` and checks a real, correctly
formed `message.tool_calls`; JSON text alone fails. This covers the suppression
case described in [Constraint Tax](https://arxiv.org/abs/2606.25605).

Unprobed protocols (including vision, long-context recall, cancellation,
parallel execution, resume and prefix cache) remain unknown. They remain eligible
as unverified in advisory mode and are ineligible when required in strict mode.
This patch does not claim that a short battery demonstrates those capabilities.

The concrete adapter reads `/api/version`, `/api/tags`, and `/api/show` before and
after inference. Digest/version metadata comes from the local Ollama endpoint;
the tokenizer binding includes the entire model artifact digest. The `hardware`
identity field is explicitly a hash of the local host and origin, **not** GPU
placement attestation: Ollama's metadata does not expose a physical device ID.
This trusts the local Ollama service and host-owned evidence file, not a hostile
local administrator. It is not a signed hardware attestation. Operator-supplied
identity files and generic probe callbacks still cannot mint measured evidence.

Results are atomically persisted without prompts or provider response text. A
failed refresh replaces that model's prior success with an unknown synthetic
record. A measured failed capability is stored as `passed: false`. Exit 1 denotes
an unavailable battery or configuration/storage error; measured capabilities can
still fail in a successful command, so inspect the per-capability JSON booleans.

## Verification and remaining qualification

Unit/contract coverage lives in `test_production_capability_evidence.py`,
`test_live_capability_selection.py`, and `test_ollama_conformance.py`, beside the
existing conformance, routing and pool suites. The probe tests use a disposable
loopback HTTP fixture, not a real model. Actual installed models still need the
operator refresh and live workload qualification; no probes were run against the
developer's model service while implementing this change.

Only two existing `server.py` call sites pass payloads to the packaged gate; its
line count is unchanged. No new server implementation or threads are introduced.

Regression coverage also lives in `test_capability_routing_modes.py` and
`test_pool_capability_modes.py`. It exercises empty-store structured/vision
availability, alternative selection after a measured failure, all-failed fallback,
strict refusal/pass behavior, off-mode bypass, stale/changed/synthetic evidence,
combined tools/schema requirements, environment selection, production composition
and the primary-only agent-decision transport path.

Local verification for the advisory revision: **193 tests passed, six server
integration cases deselected**, using the existing workspace-local harness with
inherited-ACL temporary directories and `--noconftest`. The normal pytest temp
fixture still fails with Windows `PermissionError`; this is focused verification,
not a full-suite or live-model pass. The regression was first reproduced: an
empty store returned `tier=None` for JSON-schema output instead of `code`.

`D:/sonder-eco/venv-rt/Scripts/python.exe -m ruff check` was available and run.
The repository-wide run reported 13,961 findings at that point. The new policy
modules and focused test files now pass scoped Ruff; `tier_router.py` and
`ollama_pool.py` retain their HEAD lint counts (6 and 35) with no additional
finding categories/counts. Unrelated lint debt was not rewritten.

Architecture checks, AST parsing of all 20 changed Python files, and
`git diff --check` passed. `server.py` remains 26,931 lines (cap 26,942), with no
additional edits in this revision. The refresh CLI, probe battery and atomic
store were preserved. Changes remain uncommitted; no push, PR or deployment.

Gateway latency revision: a counting fake reproduced **20 identity observations
for 10 empty-store structured requests before the fix, and zero afterward**.
`test_capability_identity_cache.py` covers the advisory fast path, required-failure
and strict TTL reuse, unavailable observations, origin/model/context isolation,
CLI refresh invalidation, strict checks after expiry/refresh during dispatch,
and advisory/strict model-name handling. The combined focused run passed **215
tests**, with the same six server integration cases deselected, using the
workspace-local `--noconftest` harness. This is gateway/contract verification,
not a live-model latency benchmark or a full server integration run. The physical
pool's separate identity policy is unchanged by this gateway fix.

