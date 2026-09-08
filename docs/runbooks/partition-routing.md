# Partition routing and synthetic cluster acceptance

`sonder_runtime.domain.scheduler_partition.PartitionRouter` is a pure,
in-process contract for a bounded scheduler-partition inventory.  A partition
is enrolled with `upsert`, and its revision must advance monotonically when
metadata changes.  Inventory pages are ordered by partition identity, capped
at 128 entries, and carry an inventory revision so a caller cannot continue a
cursor across a changed enrollment set.  Protocol negotiation is explicit and
rejects a client version that does not exactly match the configured version.

Session keys use deterministic weighted rendezvous selection across active
partitions.  The same enrolled set gives the same route regardless of
enrollment order.  A draining or paused partition is not selected.  The
router is a routing hint; it does not grant ownership, perform discovery,
replicate state, or promote a replacement controller.

`tests/test_p6_large_cluster_acceptance.py` exercises this contract with
simulated 16-, 64-, and 256-worker inventories.  The same tests use local
fake snapshot sources to verify refresh admission never exceeds eight probes,
and use injected drain dependencies to verify a bounded 64-record plan is
reported incomplete when a larger simulated inventory is truncated.

These are deterministic acceptance checks only.  They do not establish
real-node throughput, network capacity, automatic enrollment, durable
cross-node session placement, acknowledged replication, quorum, failover, or
high availability.  The production compute inventory and remote-node limits
remain those documented in [the indexed inventory runbook](compute-indexed-inventory.md).
