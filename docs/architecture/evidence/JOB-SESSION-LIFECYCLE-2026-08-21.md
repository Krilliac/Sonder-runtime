# Job/session lifecycle linkage evidence

Date: 2026-08-21

## Contract

`JobSessionLifecycleRecorder` is an injected application adapter. It maps a
linked `JobRecord` revision to `job.created`/`job.lifecycle` and a bounded
`OutputEvent` watermark to `job.output` in the parent session's append-only
`SessionRepository`. Jobs without `parent_session_id` remain unlinked.

## Idempotency and recovery

The default keys are stable (`job_id + revision` for lifecycle and
`job_id + output watermark` for output). The key is persisted in the event
payload and deterministically hashed into `event_id`. A restarted recorder
first searches durable history, so repeated delivery returns the existing
event and does not append a duplicate. `replay(session_id, job_id=...)` is
read-only and reopens the linkage from the durable stream.

## Bounds

Repository scans and replays are capped by `max_events` (default 10,000), and
serialized payload/output data are capped by `max_output_bytes` (default 64 KiB).
Spill metadata is copied as bounded JSON metadata; the recorder does not read
or duplicate spill contents.

## Verification

Focused coverage is in `tests/test_job_session_lifecycle.py`: lifecycle and
output replay across a fresh recorder, duplicate suppression, unlinked jobs,
and output bounds.
