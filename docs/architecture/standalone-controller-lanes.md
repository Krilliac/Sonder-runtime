# Standalone controller lanes

The local `agent` entrypoint (and workbench routes that invoke it) exposes
`agent_lane` to its internal model loop. The host creates one private parent
capability for that invocation, lazily on its first admitted lane command. The
model receives only `action` and `payload`; it cannot select a parent, principal,
workspace grant, approval, or bearer. Permission approval is bound to the host
run ID, effective workspace roots, and canonical command arguments. Dispatch
executes the same immutable prepared command after approval; it rechecks live
authority and rejects changed canonical workspace resolution. Bearers are
never included in tool output, model prompts, or report metadata.

Supported actions are `spawn`, `list`, `inspect`, `send_message` (`message`),
`wait`, `interrupt`, `resume`, `cancel`, `reports` (`report`), and `ack`.
Spawn requires `command_id`, `task`, and `workspace_root`. All commands use the
existing durable lane dispatcher and service, including its idempotency,
workspace overlap checks, fanout, token/step/wall limits, and execution grants.
This adds no second task registry. Paths must fall within configured
`[state].workspace_roots`, intersected with the host-selected project. A relative
child root is accepted only when exactly one effective root exists.

Lane control is unavailable in hosted, read-only, or nested model loops. Unsafe
lab mode does not grant this authority. Standalone controllers may launch
independent first-level lanes; nested child hierarchies remain unsupported.
The normal permission gate still applies to lane execution.

On normal parent return the private bearer is discarded. Children remain
independent under their existing bounded grants, and their durable reports and
user-facing control remain available. Explicit controller cancellation requests
cooperative child cancellation before any additional parent model/tool action.
Cancellation cannot prove an already-running external effect has stopped.

There is currently no trusted durable standalone continuation identity. A new
unrelated agent invocation cannot reclaim an old parent's capability using a
model-provided session ID. Users can continue managing the existing lanes through
the authenticated user surfaces.

Any run that admits or ambiguously attempts spawn, steering, or resume reports
`delegated-work-verification-required`. Its summary is explicitly unverified;
child output and parent checks racing child writes do not certify completion.
Normal completion and every returned early outcome (including model/parse errors,
step exhaustion, and missing evidence) include the same delegated-work standing.
Existing failure markers and failed validation receipts remain failures.
The report includes host-generated JSON with run ID, parent ID, and exact child
IDs/revisions/statuses (or an explicit unavailable state). A future verifier must
establish quiescence and validate the same durable revisions before this
restriction can be relaxed. The current slice does not implement that verifier.
