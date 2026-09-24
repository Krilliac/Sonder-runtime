# Issue #510: delegated fan-in artifact readiness

The production `master_orchestrator.run_delegated()` fan-in already validates
each worker's completed output against its readiness receipt before sending
outputs to the audit/aggregation stage. This slice strengthens that boundary
without changing the older `ArtifactReadinessBarrier.join()` callers.

Each new worker receipt records the run/producer-derived artifact ID, byte
length, content SHA-256, completion marker, and the SHA-256 of its delegated
source task. Repository-target bytes are checked separately by the existing
before/after `fleet_provenance.validate_delegation` calls. The receipt also
holds a digest of the worker's completed output and successful host checks.
At fan-in the host recomputes the expected task SHA and verifier receipt from
the retained output and assigned objectives; joining rejects a changed ID,
length, task digest, verifier receipt, content digest, stale timestamp, or
partial producer set. The digest is an integrity correlation, not a signature
or independent authority over the host.

`tests/test_artifact_readiness.py` exercises metadata mismatches directly and
mutates the producer receipt in a real delegated join. The audit callback
remains uncalled after each mutated size, task digest, verifier receipt, or
completion marker. The focused local regression suite also exercises the
existing EVAL-006/007 failure linkage.

This covers the delegated-fleet text output boundary. It does not yet bind
strategy hypotheses, recursive child artifacts, externally stored files,
or Git commit identities; their consumers need their own verified manifests
and trusted source revisions before joining those artifacts.
