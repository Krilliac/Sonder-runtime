# Private REPL conversation lifetime

An actual REPL invocation owns one private selected-conversation slot. Repeated
`/work` commands in that selection reuse the original authenticated context,
selector and durable parent attachment. They do not register another parent,
change the canonical memory ID, renew expiry or increase tool/root ceilings.
The slot is not a public model argument or a process-global lookup by chat ID.

Each work turn has a new controller, private command issuer, run ID and bounded
durable ordinal. Closing that controller fences the turn's commands, callbacks
and approvals; the conversation owner remains held. Automatic model escalation
stays inside that one turn. `/new`, `/resume`, workspace selection/creation/clear,
and REPL exit close the selected lifetime. Concurrent or nested controllers
cannot borrow a turn by copying its run ID or operation context.

The final host observation ledger and exact output are captured through the
existing trusted projection codec even when no child was delegated. The next
turn inherits ordered parent observations; an earlier failed or unvalidated
effect cannot disappear into a new clean ledger. Missing terminal capture or
corrupt projection refuses a new turn. Required evidence and existing bounds
remain conservative.

A delegated turn may advance only after its exact certificate and terminal
receipt are durable and current. Admission checks the original parent/grant,
generation, exact child set, workspace exclusion, certificate/receipt digests,
and current provider cleanup proofs. Terminal status alone is insufficient.
The old pending identity and receipt remain in immutable retained history before
the new generation is admitted. A new turn cannot replace an approval-pending,
approval-unknown or effect-ambiguous result. All transitions stay under the
original root attachment and original authority expiry.

There are at most 32 retained turns per conversation lifetime, in addition to
the existing bounded observation ledger, projection and output-store quotas.
No retained terminal data is evicted or deleted. Reaching a bound refuses new
work; it does not silently drop evidence.

`/recover resume <continuation-id> <command-id>` remains a separate explicit
fresh-attachment flow. It refuses a currently owned root. To release the current
live selection first, explicitly reselect its persisted conversation with
`/resume <session-id>`; this closes the old lifetime. This slice does not resume
model execution after process restart or reconstruct missing native cleanup
proof. Original pending verification recovery remains supported.

Acceptance covers two actual `/work` commands through the console and a loaded
Application, plus two independently certified turns over real contained test
processes in a disposable Git repository. Model responses are deterministic
scripts; no live-model or remote-execution success is claimed.
# Exact terminal history

Each repeated managed turn retains two distinct records. The original host
projection stays immutable and remains the input to delegated verification.
The final receipt binds that original digest to exact outward output, including
host validation warnings, escalation text, end reports and activity reports,
plus typed mutation/validation and certificate observations. `UNVERIFIED` is
an explicit failure class and cannot become clean through persistence.

Controller close fences the turn before outer report formatting completes.
The private conversation boundary persists the final receipt and closes the
durable turn before returning the completed text. Missing, corrupt or failed
final persistence prevents advancement; it does not discard original evidence
or rerun a model. Retained history keeps both records. Older turns without a
final receipt are not silently upgraded or accepted for advancement.


## Permission discovery and staged recovery approval

The actual slash-command dispatch remains in `main`; a wrapping decorator owns
the conversation lifetime. Static command discovery therefore sees the same
branches the operator invokes. `/recover` declares `workspace_run`, matching its
inner prepared attachment and verification approval policy; a missing or changed
catalog marker refuses before dispatch. The outer gate does not spend a coarse
approval ahead of either exact prepared command.

A recovery retry waiting for verification approval returns
`VERIFICATION_APPROVAL_PENDING` and the exact persisted approval call ID. The host
reloads scoped recovery metadata after the attempt and requires the same original
pending identity, phase and code before displaying it. Metadata disagreement
remains unavailable. No pending result publishes the original output.

Each invocation releases its attachment before returning. If attachment approval
is issued first and verification approval later, a subsequent invocation requires
fresh attachment approval for its new ownership epoch. The original attachment
nonce cannot be reused. Repeating the original recovery command preserves the
pending verification identity; after current attachment and original verification
approvals are available, the test runs once and the original result is published.
