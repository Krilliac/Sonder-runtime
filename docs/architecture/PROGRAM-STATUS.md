# Sonder Runtime Architecture Program — implementation status

Tracks the implementation of the approved architecture program
(SPEC-1 review → SPEC-2 production readiness → SPEC-3 refactoring →
SPEC-4 signed distribution) in this repository. Updated with each
program commit on this branch.

## SPEC-2 — Production Readiness: implemented

| Work package | Status | Where |
|---|---|---|
| WP1 production baseline | done | sonder_version/config/preflight/service_state/logging/metrics/operations_store/shutdown, `python -m sonder_runtime` |
| WP2 configuration + secret safety | done | sonder_config (fail-closed, all-errors, secrets never in TOML), sonder_secrets rotation with overlap expiry, redacted `config`/`diagnostics` |
| WP3 health + lifecycle | done | /live /ready /health /version /metrics, Ollama dependency probe, sd_notify, drain on SIGTERM and POST /v1/admin/drain |
| WP4 admission + error contracts | done | sonder_lifecycle: concurrency slots, queue depth, admission deadline, drain/maintenance rejection, auth-failure limiter, error envelope + correlation IDs |
| WP5 schema/migrations | done | checksummed ledgers, migration lock, future-schema + edited-history refusal; adoption baselines for memory/autopilot/fleet |
| WP6 operations events + metrics | done | operations.db events (redacted), Prometheus metrics with no-op fallback |
| WP7 backup/restore | done | online-backup snapshots, manifests, verify, GFS retention, restore smoke, systemd timers |
| WP8 packaging | done | install_sonder.sh / uninstall_sonder.sh, versioned releases, hardened units, reverse-proxy reference |
| WP9 upgrade/rollback | done (consolidated into SPEC-4 engine) | sonder_update_engine |
| WP10 runbooks + acceptance | runbooks done; acceptance harness = tests/production (121 tests). A disposable-VM harness remains future work. |

Known limits: request deadline/cancellation propagate to admission and
drain but not yet into every model-call internals; the OpenAI-error
shapes for chat remain legacy-compatible alongside the new envelope.

## SPEC-3 — Refactoring Roadmap: foundation in place (Phases 0–2)

| Phase | Status |
|---|---|
| 0 characterization | The 2,500-test suite is the behavioral baseline; goldens for chat/memory flows exist in tests/. |
| 1 skeleton + composition root | done — sonder_runtime/{domain,application,adapters,platform,bootstrap}, `bootstrap/app.py` builds the Application graph lazily; no import-time side effects (CI-checked). |
| 2 runtime-policy extraction | done — pure rules in domain/runtime_policy/rules.py, atomic JSON + file lock in adapters/filesystem/atomic_json.py, root runtime_policy.py delegates with identical names/behavior (102 policy tests unchanged). |
| 3 model gateway | done — adapters/ollama/gateway.py implements the ModelGateway port over the legacy transport: context-level cloud-consent gate (Forbidden without explicit consent), driver ModelCallError mapped into the domain taxonomy, bounded-retry/single-attempt semantics delegated to the transport that owns them. Wired as the composition root's gateway. Call-site migration off direct transport calls continues incrementally. |
| 4 memory | ports defined; extraction pending. |
| 5 execution | ToolExecutor port defined; extraction pending. |
| 6 automation | ports defined; state-machine extraction pending. |
| 7 training | pending. |
| 8 thin transports | pending (entry module counts as CLI adapter in the checker until then). |
| 9 legacy import removal | pending. |
| 10 enforcement | `scripts/check_architecture.py` blocking in CI for the package: layer edges, cycles, sqlite3/subprocess/network containment, no env reads in domain/application. |

ADRs 001–008 in docs/architecture/adr/.

## Post-program capability work (driven by live A/B runs)

- Wider runner languages (15 total), `data_inspect` structured previews,
  thin-client session-memory fallback, fence/prose-tolerant agent JSON
  parsing — see the capability commits on this branch.
- Speculative execution + branch prediction (sonder_speculation.py): a
  history-indexed next-tool predictor (measured 100% accuracy on
  repetitive agent workloads after one warm-up run), read-only-allowlist
  speculative dispatch with retire/squash semantics, an argument-level
  file stream-prefetcher (predicted file_read calls retire with each
  file dispatched exactly once — proven by integration tests), and
  speculative model prewarm overlapping cold load with host-side work.
  Honest measured end-to-end gain on the CPU sandbox: ~0% (speculated
  tools cost milliseconds against multi-second decisions); the win
  scales with slower tools (big repos, network filesystems) and faster
  models (GPU), with /status exposing accuracy and retire rates to show
  when a deployment crosses into the paying regime.

## SPEC-4 — Signed Engine Distribution: Linux reference engine implemented

Implemented: updates.db (plans, step journal, releases, trusted roots,
channels), validated state machine with CAS revisions, bundle builder +
manifest (per-file hashes), adversarial-safe extraction, compatibility
refusal, confirmation nonces, maintenance-lock + backup + drain +
target-release migration + health gates, atomic pointer switch, retained
previous release, operator rollback with missing-release refusal,
offline import, audit events, admin status route, CLI.

Trust: python-tuf verification path is wired for bundles carrying TUF
metadata; the unsigned path requires an explicit double gate
(--allow-unverified + SONDER_UPDATE_ALLOW_UNSIGNED=1) and is documented
as non-production.

Publisher pipeline (WP7): `tools/tuf_repo.py` initializes a TUF repo with
the SPEC-4 role thresholds (root/targets 2-of-3, snapshot/timestamp
1-of-1), signs a built bundle's archive as a target, and assembles an
offline bundle the client verifies through its own trust chain. Proven
end to end: a signed bundle imports through the real update engine with
NO unsigned gate; tampered targets, below-threshold and fully-forged
metadata, and unsigned targets are all rejected. Building the publisher
also fixed a latent client bug — the TUF verify path used python-tuf's
HTTP-only fetcher and so never worked for offline file:// bundles; a
filesystem fetcher that maps missing files to 404 now drives the
root-rotation walk correctly. Signing ceremony: docs/runbooks/
publish-release.md. Optional deps pinned in requirements-update.txt.

Remaining for full SPEC-4 sign-off: resumable online downloads,
Windows/macOS activation helpers (M6), and the Flutter System page UI
(M5) — the status API it polls is in place.
