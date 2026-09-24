# Issue 510 continuation qualification

Continuation baseline: `65f64a6ba4ce8e6ea22fcc335f10b803633c4454`.
This record separates demonstrated integration from model, platform, and
promotion qualifications that remain open. It does not check master-spec items.

## Authenticated learning

The real failed-check dispatcher path exposed a missing current-owner attachment
when validating a negative verifier receipt. The terminal eligibility adapter now
uses the bound owner's scope for that check. Failure remains an unknown work
completion, while its trusted negative observation can be retained and demote
learning; it does not become a successful work result.

`test_managed_learning_principal_qualification.py` registers and authenticates two
principals, enrolls and selects their app-control bindings, and invokes the real
managed dispatcher, verifier receipt producer, composed authoritative source, and
application unit of work. Two receipts from the first principal remain a
candidate. A matching receipt from the second principal promotes the subject;
a real failed verifier check demotes it. Reopening the database retains all four
observations and the demotion. Each dispatcher has its own control/lane store;
the admitted workspace and authoritative application store are shared. This does
not simulate the authenticated identity by constructing a verifier authority.

Managed receipts use an admitted absolute workspace scope. The replication
domain and configuration grammar now accept bounded POSIX, drive, and UNC
spellings within the existing 256-character ASCII grammar (no spaces or Unicode)
as exact opaque scope identities. No path normalization or filesystem
authorization is inferred; different spellings remain different scopes.

`certified_after_return` is still not learning evidence. Independent principals
do not prove independent model families or statistical independence. Facts do
not promote to policy without the separate evaluation gates.

## Context and memory selection

`test_live_agent_context.py` runs the real tool-enabled agent request builder and
persists its model request in the canonical session outbox. A fresh interpreter
replays the exact stored sections and prefix manifest after project rules and
skills change on disk; a new request observes the changed inputs. Provider and
artifact identity changes invalidate the prefix, including unavailable identity.
These tests use provider doubles and establish request identity and replay,
not live provider cache performance or a shared process-independent cache.

The acceptance audit reproduced a truncated `SKILL.md` being silently omitted
while the live prefix still claimed completeness. Scoped live discovery now
requires every selected skill manifest to have valid, complete metadata within
16 KiB. Invalid initial discovery or partial refresh exposes a content-free
diagnostic and makes no prefix-cache claim. Last-good records remain available
as incomplete state, and higher-precedence damage cannot silently select an
older lower-precedence skill. Missing explicitly configured roots also refuse;
intentional individual-manifest removal still refreshes normally. Generic skill
discovery keeps its permissive behavior for callers outside the live producer.

Same-project, irrelevant task-family memory is excluded through retrieval and
the actual Autopilot pre-model callback. Only the selected reference receives
response-bound attribution; a refusal before a response does not penalize it.
This is a negative selection control, not a held-out task-quality ablation.

## Shared physical request admission

Explicit host burst/rate limits share a SQLite bucket across processes using one
state home. OS file locking covers first file creation as well as the SQLite
transaction; a persistent, fsynced initialization marker distinguishes a missing
database from first boot after restart. Damaged or missing initialized state
refuses sends. Clock rollback does not refill tokens. Mixed configurations retain
the lower burst and refill rate until an explicit stopped-host state reset.

The cold-start two-process regression initially failed, then passed after the
creation lock. A second regression reproduced a deleted database minting a new
burst after restart; the persistent marker closes that case. The database,
SQLite sidecars, and lock are in the live control-plane inventory; real ordinary
file and SQLite mutation tools cannot reset their capacity. This is cooperative
host-process coordination, not protection against arbitrary same-user host code.
Native Windows lock behavior requires hosted CI. Batching/coalescing is separate.

## Effect recovery

ToolGateway now records uncertainty when redaction, receipt construction, or
outcome publication fails after an effect runs. A committed definitive outcome
is retained. If uncertainty publication also fails, the original exception is
preserved; a storage outage is not evidence of successful reconciliation.

`test_tool_gateway_effect_crash_matrix.py` kills child interpreters before,
during, and after a physical effect, after its receipt, and after its checkpoint.
It also exercises overlapping effects and the settled checkpoint prefix. These
cases invoke the actual gateway with an effect-counting invoker double and a
persisted journal. Separately, real composed legacy and native typed file writes
now carry their canonical descriptor effects into the journal. Read-only and
denied requests do not create mutation intents; explicit restricted scopes stay
restricted. Static patch descriptors conservatively journal preview requests.

`test_typed_file_effect_crash.py` also kills a child immediately after an actual
composed typed append returns from the filesystem adapter but before its receipt.
Reopening the journal under a new owner leaves the effect uncertain and refuses
a second real append; the target retains exactly the first mutation.

Local compute-submit and its nested process-start can now reconcile from a
terminal durable attached-process registry record. Exact worker/controller/job,
idempotency, scope and respective request digests must match. Real subprocess
crash tests cover both receipt boundaries and prove no second launch. Completion
proves the launch effect, not workload success; a running, legacy, missing or
unattached record remains fenced. The registry is a trusted host writer, not an
independent OS liveness oracle.

The child-sessions checkpoint repository still has no journal-high-water saga.
Whole-child effect fencing prevents duplicate execution but does not prove that
every resumed child checkpoint contains a particular settled effect prefix.
Unknown compute-cancel, subagent, and self-mod reconciliation must stay fenced;
terminal status text is not a receipt. These are reasons to keep #515 open.

## Chat, backend diagnostics, and isolation

Chat is a policy lane mapped to general by default, without a new model tier.
Typed and HTTP requests preserve explicit pins and the operator's strict alias
contract, including mixed provider bindings. Content-free lane/reason metadata
and the selected route survive canonical session replay. Chat-to-work admission
uses the existing authorization and idempotency boundary and one classification.
Owner-scoped HTTP handoffs now retain a verified prior model-response event
reference and append content-free admission/return events. JSON and SSE receipts
expose durable event IDs and truthful returned/refused/unknown status; returned
means the handler returned, not successful task completion. Restart and account
isolation tests cover the existing idempotency boundary. The source reference
is provenance only: legacy work lanes do not yet consume prior chat context or
enforce separately structured criteria.

OpenAI-compatible CLI probes now remain synthetic diagnostics even when a real
transport and an operator identity file are supplied. A stable identity file and
provider model label do not attest weights, tokenizer, template, context, or
hardware. Tool fallback, sequential tools, continuation, and in-flight
cancellation remain unknown without a real granted runtime protocol path. These
records cannot authorize capability routing or promotion.

The Linux Codegen build adapter is opt-in, pins a local image ID, stages only the
intersection of declared sources and host-granted inputs, and uses a read-only
source mount plus bounded disposable build memory. It refuses source/root
replacement races, uncertain launcher exits, and unverified container cleanup.
Windows and unconfigured hosts remain closed. The dedicated native container
workflow must pass its non-skipped probes before native containment is claimed.
Build output is candidate-controlled diagnostic data, not an independent grade.

The self-mod literal-case projector leaves fixture/setup-dependent held-out
suites unevaluated, including suites with an applicable conftest. This does not
create an independent grader. The confidential evaluator, independent task
corpus, live benefit ablations, and promotion authority remain separate open work.
