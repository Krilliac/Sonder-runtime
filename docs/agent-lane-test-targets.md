# Optional test execution for local agent lanes

Interactive lanes default to file tools. A host can additionally configure
`run_tests` through `SONDER_LANE_TEST_TARGETS_FILE`, an absolute path to a JSON
catalog outside every delegated workspace. Application composition loads it
when the lane service is first accessed.

```json
{
  "targets": [
    {
      "name": "unit",
      "workspace_root": "C:/work/example",
      "argv": ["C:/tools/python/python.exe", "-m", "pytest", "-q", "tests/unit"],
      "timeout_seconds": 30,
      "max_descendants": 4,
      "memory_limit_bytes": 536870912
    }
  ]
}
```

The executable must exist at an absolute path outside every configured
model-writable root. The catalog must also be outside every such root, including
roots used by other lanes or ordinary file tools. These checks run at composition
and again during execution.
The workspace must exist and be absolute. A catalog contains at most 16 targets
and 64 KiB. Limits are at most 600 seconds, 16 descendants, and 4 GiB per target.
The serialized command snapshot is bounded to 4 KiB.
Use dedicated test commands; do not put secrets in command arguments.

A model can request only
`{"tool":"run_tests","arguments":{"target":"unit"}}`. It cannot supply a
command, environment, workspace override, or resource limit. The chosen target's
workspace must exactly match the lane's exclusive workspace grant. The model
receives the configured target names in its tool schema.

Configuration alone does not authorize execution. The composed tool gateway
also applies the current operator permission policy under `workspace_run`,
which must permit the requested host execution. Nothing here changes that
policy. With no catalog, an empty catalog, or without an explicit tool grant,
`run_tests` remains unavailable. Programmatic hosts can use
`compose_lane_test_tools` with a validated `LaneTestCatalog` and their existing
durable process provider; custom lane services must explicitly include
`run_tests` in their allowed tool subset.
Approval receives the host-resolved argv, canonical workspace and command/catalog
digests. Durable job metadata retains the bounded command snapshot and digests;
receipts retain digests and canonical workspace.

This is **host execution, not a filesystem sandbox**. Repository tests are code
and execute with the runtime host's operating-system rights. A workspace grant
constrains typed file operations and the selected working directory; it does not
contain filesystem or network access performed by test code. Enable this
feature only where the operator has authorized executing that repository.

Commands use the existing durable process provider with a replacement environment,
required native containment, resource controls, and process-tree cleanup. If
strong native containment is unavailable, no process starts. The environment
contains only a constructed executable/system PATH, system launch directory on
Windows, temporary directories, and fixed locale/Python encoding settings.
Ambient credentials, proxy/SSH context, Python search paths, and runtime controls
are not inherited. Catalog-defined environment variables are not supported.
This environment reduction does not provide filesystem or network isolation.
Lane cancellation,
original grant expiry, parent-grant revocation, and catalog changes cancel active
tests. Catalog contents are hashed at composition and checked before and during
execution; changing the catalog requires host recomposition. Lane resumes retain
their original expiry and cannot reset authority.

Receipts record the job ID, exit status, bounded redacted output and cleanup
evidence. A returned completed effect remains completed if cancellation arrives
after the effect; a cancelled job with proven cleanup retains a cancelled receipt.
Subsequent calls still undergo cancellation and permission admission checks.
Unresolved cleanup remains an uncertain effect and cannot be automatically
replayed.

The repeatable acceptance uses a **scripted model**, actual typed file operations,
and real subprocess tests in a disposable Git repository. It demonstrates an
initial failing pytest run, an exact file edit, passing tests, and a reviewable
diff. A separate subprocess exits after recording a model-returned test request;
reopening the service consumes the saved request once. These checks establish
runner mechanics, not the coding quality of a live model or distributed recovery.
