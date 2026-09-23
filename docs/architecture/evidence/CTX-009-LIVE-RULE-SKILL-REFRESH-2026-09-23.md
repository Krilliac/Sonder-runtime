# CTX-009 live rule and skill refresh evidence — 2026-09-23

## Scope

This revision records a bounded local acceptance probe for PR #537 at branch
head `338a988fa5f13b40fb9a50ce48e97efa9b1d504f`. The requirement remains
`implemented_unverified`.

## Reproducible procedure

1. Confirm loopback Ollama availability and select the configured local model.
2. Create temporary SQLite session/lane stores and a temporary project with
   one `AGENTS.md` and one valid `SKILL.md`.
3. Construct `AgentLaneService` with `LiveAgentContextProducer`,
   `ContextPlanningFacade`, and the Ollama gateway. Submit two identical
   requests through the lane request builder and provider gateway.
4. Change only the skill manifest description and submit a third request.
5. Record application cache observations, prefix/replay hashes, and provider
   telemetry. Do not retain prompt, response, or temporary filesystem text.

## Sanitized observations

All three requests used the same resolved model identity. Hashes below are
opaque SHA-256 values; no prompt, response, private path, or credential is
included.

| Request | Application result | Application prefix key | Replay manifest digest | Provider prompt tokens | Provider cached prompt tokens |
| --- | --- | --- | ---: | ---: | ---: |
| 1 | `miss/cold_start` | `a77468b6b4c93c59e7591147432bf310bb47b5d0fe9fbff7f543efc1f78b217d` | `c1d8cea904d0bad6e1e873df6811c5274df5058d4e9c96020999fb67fd128d65` | 150 | 0 |
| 2, identical stable inputs | `hit/hit` | `a77468b6b4c93c59e7591147432bf310bb47b5d0fe9fbff7f543efc1f78b217d` | `69db6d24420e65badd33cdea1124298c7778e4e37f4c65223f2160cc79d1dd3c` | 150 | 149 |
| 3, skill manifest changed | `miss/prefix_changed` | `024a66a2e7baf27ca81e10d9e4199914348b83a05149d9b6d41c54285408b6ea` | `b778e485c440a1b8b94f3b0740cf1bc44ee091ed7eef0f98909d4f9246f5140e` | 151 | 127 |

The first and second application prefix keys match. The third key and replay
digest differ after the skill update. Provider telemetry is the Ollama-reported
`prompt_eval_count` and `prompt_eval_cached_count` forwarded by the real
transport path.

## Boundary and limitations

The probe used only temporary session/agent state and a temporary scoped
project; it did not mutate a live database or service. It proves this local
single-worker lane path, scoped refresh, application invalidation, replay
identity, and provider-reported cache counts. Cross-process cache
coordination, multi-worker routing, concurrent model-tag replacement, and
formal CTX-009 completion remain unverified.
