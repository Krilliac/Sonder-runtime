# Work narration

Routed chat work returns a deterministic acknowledgement before starting its
worker. It names the goal, lane, known fleet breadth and worker slots, project,
configured model host, routing reason and status/cancel controls. Autopilot
initially announces that it will create its task plan; ambiguous routing names
the selected lane in an activity event once the existing router selects it.
Narration does not make an additional model request. Unknown hosts, folders and
durations are not invented. A fleet ETA requires recorded task durations and a
known slot count, and is explicitly an estimate.

The HTTP chat response carries `sonder_receipt.chat_work.acknowledgement` and
the existing opaque work-run ID/URLs. The response is written before the
deferred worker starts, including the normal assistant text delta for SSE
requests. This transport ends the acknowledgement response as usual: there is
no existing background-session SSE subscription to extend. Subsequent updates
use the existing status polling surfaces.

Natural-language model fanout retains its synchronous JSON answer through the
HTTP-authorized internal handler. Its fanout receipt adds progress without
replacing the existing answer with a deferred work-run admission.

`GET /v1/work-runs/<id>` and the work-run list add:

- `progress`: at most 24 records, newest last. Each has `id`, `run_id`, `text`,
  `kind`, epoch-seconds `at`, and `final` (an urgent transition, not necessarily
  the end of the whole run).
- `progress_complete`: whether the parent and its linked children have stopped.
- `final_summary`: recorded output/location, failures and actual validation
  evidence where available. A returned agent response alone is not validation.
- `acknowledgement` and `narration`: the persisted acknowledgement and bounded
  links to host-created source IDs. Progress itself is projected, not stored.
- `result_receipt`: the existing source/admission/return provenance fields once
  the background lane returns; those facts do not exist at acknowledgement time.

Fleet, autopilot, activity, model-fanout receipts, and administrator
`/v1/sonder/status` also add bounded `progress` lists. Progress uses the existing
fleet/autopilot event tables and activity ring. Ordinary transitions are limited
to one per ten seconds per run; task completion and failure bypass that limit.
Tokens, model-call completion noise and reasoning are excluded. Event retention
limits still apply; narration is a compact status view, not a lossless log.

Exact work-run owner lookup precedes projection or linked cancellation. Activity
IDs are process-qualified, so a restarted process cannot attach an old receipt
to a new user's activity. Detail-disabled activity stays detail-disabled after
its response falls out of the latest-response slot. Existing administrator and
developer endpoint gates remain in force.

The Flutter conversation displays the acknowledgement and one live progress
block through its existing work-run poller. It continues polling if a parent
has returned while a linked fleet/autopilot is still running, then replaces the
block with acknowledgement, final summary and the existing result. Old servers
without progress fields retain the previous behavior. Plain chat is unchanged.
The REPL prints the acknowledgement before entering a work lane and follows
linked events until completion or REPL exit.

Cancellation remains cooperative: in-flight model calls may finish, and the
existing work fence refuses later effects. A retry with the same HTTP
idempotency key returns its cached admission in the same process. The durable
replay guard treats a detached admission as uncertain across process restarts;
inspect the work-run status instead of assuming that admission meant completion.

Validation: `tests/test_work_narration*.py`, `tests/test_http_work_narration.py`,
`tests/test_http_command_narration.py`, and the affected HTTP work/session suites;
Flutter `chat_work_run_test.dart`, `chat_classify_metadata_test.dart`, and
`work_runs_test.dart`. See the lane's `NOTES.md` for executed versus blocked checks.
