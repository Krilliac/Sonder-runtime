# Job registry/session lifecycle composition evidence — 2026-08-21

The typed `JobRegistryService` now accepts an optional
`JobRegistryLifecycleAdapter`. The adapter delegates registry revisions to the
existing bounded, idempotent `JobSessionLifecycleRecorder`; it does not own job
state or alter the `JobRegistry` port.

`build_application()` composes the adapter with the same lazy
`SessionRepository` factory used by the canonical session service. `create`,
successful `finish`, and `cancel` transitions record their returned
`JobRecord` revisions after the durable registry operation. Unlinked jobs stay
no-ops, repeated revisions replay rather than append, and output bounds remain
owned by the recorder.

Focused evidence:

- `tests/test_job_session_lifecycle.py` verifies adapter ordering and replay
  idempotency.
- `tests/test_composition_job_registry.py` verifies the typed composition root
  injects the adapter and shares the session store.
- Existing `tests/test_job_service_process_cleanup.py` and
  `tests/test_wp3_seam010_jobs.py` cover the unchanged service contracts.

Validation command:

```text
python -m pytest -q tests/test_job_session_lifecycle.py tests/test_composition_job_registry.py tests/test_job_service_process_cleanup.py tests/test_wp3_seam010_jobs.py
```
