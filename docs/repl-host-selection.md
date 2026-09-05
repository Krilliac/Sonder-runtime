# Private REPL host selection

`bootstrap.repl_host_selection.ReplHostSelectionAdapter` is an internal dependency
for trusted local REPL composition. It does not modify the REPL or expose a tool.
Only source `repl`, auth level `local`, principal `owner`, and a live operation
context are accepted. The legacy memory sessions table is a single local-owner
history store; this is not multi-account authorization.

Inject `get_session(exact_id)`, `find_session(operator_query)`,
`touch_session(exact_id)` and `policy(context, exact_id)`. Bind the first three
to the configured memory store connection at trusted composition. The policy
callback must validate live operator project selection and complete private-state
exclusion, and return frozen `ReplHostPolicy(grant_id, revision, expires_at,
workspace_roots, allowed_tools)`. Grant IDs/revisions must come from stable host
policy, never session updated timestamps, random run labels, or model arguments.
This module does not create a durable host-policy registry or infer one.

`create(exact_id, context)` persists via touch then re-reads the exact canonical
16-character lowercase hex session row. `select_exact` requires the row already
exist. `select_resolved(query, context)` resolves a bounded operator title prefix
once, then re-reads and binds only the returned exact ID. These are private host
operator actions, not parsing routes for model-produced slash commands.

Use `with adapter.scope(selection, context)` around the root host call and inject
`adapter.authorize` into the continuation service. Authorization requires that
private context scope and the current exact issuer-owned selection object; an ID
or copied dataclass alone is insufficient. Each authorization re-reads the exact
session and retains the original scope context as its authority ceiling. The
original context must remain live; supplied contexts must preserve cancellation,
source, principal and auth level, and cannot expand deadline, roots or cloud/remote
flags. Narrowed contexts are accepted. Each authorization re-reads the exact
session and live policy. A live grant may attenuate the original tool/root ceiling
and shorten expiry; increased policy expiry is clipped to the original expiry.
Changed grant ID/revision fences the selection. Roots must be existing canonical
absolute directories, ordered and unique. Tools must be bounded, ordered and
unique. Current context roots must cover the policy roots.

`clear()` and every successful reselect invalidate previous selection epochs.
Scope cleanup restores prior context but cannot revive an invalidated epoch.
Context variables isolate concurrent execution contexts; separate threads must
explicitly enter the trusted scope. The local lock linearizes selection changes
and grant issuance; callers must still guard every actual model/tool admission
with the existing bound continuation check. No post-issuance effect lock or remote
identity is claimed. Restart requires a fresh explicit local operator selection
and the separate continuation prepare/approval/reattach sequence.
