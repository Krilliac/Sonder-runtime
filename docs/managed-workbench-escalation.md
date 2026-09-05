# Managed workbench escalation

The local automatic workbench ladder has one private managed controller lifetime.
The host issues a single-use rung permit for each sequential top-level attempt,
bounded by the existing escalation limit. Nested model/tool loops cannot borrow
that controller. Every attempt retains the original operation context, deadline,
cancellation token and managed continuation; the runtime closes it once after
the outer plan ends. Per-rung model step limits retain the existing workbench
semantics; the plan does not renew the original authority deadline.

All rungs append to one bounded host observation ledger. A failed model attempt
adds a host-classified failure observation with argument/output digests. Actual
effects and covering validation stay ordered across attempts. A later prose-only
answer therefore cannot erase an earlier unvalidated write. Required-evidence
blockers from earlier attempts remain conservative blockers for this slice.

Intermediate terminal rendering cannot seal a terminal projection or start
delegated verification. The selected final callback runs once under the live
controller and produces the original terminal projection from the accumulated
ledger. A pending or unknown approval result is never followed by another rung
or automatic finalization retry. Explicit durable recovery remains a separate
host action. Fatal callback failures cancel the controller and close its host
attachment; ordinary close does not cancel independently granted children.

Cloud/read-only restrictions still disable lane command preparation/execution.
The managed host-current check is separate, so those restrictions do not by
themselves disable otherwise authorized parent model calls. Existing cloud
consent, disclosure, tool restrictions and output budgets still apply.

Acceptance uses deterministic scripted model replies through the real workbench
loop, an actual disposable file edit, and a real managed continuation repository.
It does not claim successful inference by a live model or remote execution.
