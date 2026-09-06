# Model deployment admission

`ModelDeployment` is the immutable identity for a model backend, artifact
bundle, runtime configuration, and rank topology. Its `reservation_group` is
an identity field; it does not, by itself, reserve worker resources or prove
that a backend is ready.

`DeploymentAdmissionService` is the small application seam that composes that
identity with the existing `WorkerCapacity` port. The caller supplies one
`DeploymentResourceRequest` for every manifest rank. Each request names the
exact rank, its worker budget, and an explicit memory demand. The service
checks tuple shape, rank order, rank-to-host binding, and bounded demand before
calling the worker.

Admission computes a digest over the immutable deployment and the complete
resource plan. It then derives one stable worker job identity per rank,
reserves each lease, and dispatches each lease before publishing a redacted
`DeploymentAdmissionReceipt`. Repeating the same deployment and plan within
the process is idempotent. A different deployment or plan cannot reuse an
active reservation group. A single worker-capacity port can be used for a
single-host deployment, or a bounded mapping from host ID to independent
worker-capacity ports can be used for a multi-host deployment. Every target is
resolved before the first reservation so a missing host authority fails
without a partial admission.

If a later reservation or dispatch fails, the service attempts cleanup for
every worker identity touched by the call and returns no receipt. Existing
worker semantics remain authoritative: an undispatched lease may remain until
its bounded expiry because `release_capacity` only releases dispatched work.
The service does not invent immediate cleanup proof.

`release(receipt)` requires the exact live receipt issued by this process and
delegates cleanup to the worker for every rank. A cleanup failure retains the
local admission entry so an operator or owner can retry. `reconcile()`
delegates to the worker's bounded reconciliation pass and reports expired
worker identities plus any active plan that contains one; it does not retry,
promote, or silently remove an affected deployment.

This seam deliberately does not select nodes, start or health-check a model
backend, move model weights, implement model sharding, persist its own
admission index across restart, or provide fencing, quorum, consensus, or
high-availability behavior. A durable adapter and the existing placement,
provider, ownership, and artifact contracts must supply those separate proofs.
