# PR 661 CodeQL static triage

Reviewed on 2026-10-04 against PR head
`ee428fc2e2e8373347a9077a32c4f41d545322b5` and main
`7c3e47c7e3e230e21bfb749efc714af38f03b18a`.
The [aggregate check](https://github.com/Krilliac/Sonder-runtime/runs/111399531920)
reports 33 annotations: 31 high and 2 medium. Successful Analyze jobs do not
resolve these annotations. Its summary explicitly notes that alerts absent
from the previous analysis can reflect the size of the change rather than a
new defect.

No introduced defect was established by this static review. All 33 annotated
sink lines are identical to main. Twenty-two annotations occur in files whose
entire contents are identical to main; the remaining eleven have the exact
line mappings below. Unchanged sinks alone do **not** prove an alert harmless:
a changed source or caller could create a new flow into an existing sink.

## Evidence and limits

The check-run annotations API was readable. The Code Scanning alerts API
returned HTTP 403, `Resource not accessible by integration`; annotations have
no raw details and their numbered source links do not expose the source-flow
locations. Consequently, this review cannot reconstruct each reported secret
source or definitively explain why CodeQL first reported it on this head.
The config change against main adds a workspace-root environment setting;
the canonical secret environment/redaction policy is unchanged.

Review used source comparison, call sites, and the boundaries in
[SECURITY.md](../../SECURITY.md). Operator configuration and private runtime
directories are trusted inputs under that policy. No crafted requests,
vulnerability reproductions, or runtime attack checks were used. No alert was
dismissed, no query was suppressed, and no CodeQL check was changed.

Numbers below are the zero-based annotation positions in the check response,
preserving both separate annotations at launcher line 105. `Not actionable`
means the cited predicate/control defeats the annotated claim in the reviewed
supported path. `Needs source trace` keeps an unproven secret-flow claim open;
it is neither a confirmed leak nor a blanket false-positive classification.
Paths are relative to the repository root; `head -> main` records identical
source text at the indicated lines.

## Annotation assessment

| Annotation(s) | Path and identical sink lines, head -> main | Assessment and control |
|---|---|---|
| 0 | `tests/test_deployment_safety.py`, 46 -> 46 | Not actionable. A test asserts that the expected URL occurs in deployment configuration text; it is not an executable URL sanitizer or access-control predicate. |
| 1, 2 | `sonder_runtime/interfaces/http/serve.py`, 3250 -> 3240; 3243 -> 3233 | Not actionable in the supported logging path. Logs contain the slash command token and permission refusal; canonical production formatters redact configured secret values and credential shapes. Arguments are not directly included in these calls. |
| 3 | `sonder_runtime/interfaces/http/serve.py`, 2007 -> 2003 | Not actionable. The auth log contains a mode and boolean authorization/key/account-presence results, not a password or key value. |
| 4 | `sonder_runtime/application/extensions/experiments.py`, 196 -> 196; entire file identical | Not actionable. `ExperimentDefinition.__post_init__` requires `fullmatch` of `[a-z][a-z0-9-]{0,31}` before the ID is joined beneath the configured experiment root. |
| 5, 6 | `sonder_runtime/adapters/inference/ollama_pool.py`, 339 -> 339; 292 -> 292; entire file identical | Not actionable in the supported logging path. Typed trusted origins are CIDRs, worker validation rejects inline credentials, and canonical log formatters redact credential shapes even for the debug call preceding validation. |
| 7 | `sonder_runtime/adapters/execution_tools/code_runner.py`, 1086 -> 1086; entire file identical | Needs source trace. The value written is the caller's requested source code in an ephemeral snippet directory. This function does not concatenate a runtime credential into the snippet. The annotation alone does not identify a concrete secret-bearing source. |
| 8, 9, 10, 11 | `sonder_runtime/adapters/debugging/launcher.py`, 649 -> 649; 642 -> 642; 627 -> 627; 212 -> 212; entire file identical | Not actionable under the trusted private-root policy. Run IDs must match `debug-run-[0-9a-f]{32}`, JSON names have an exact allowlist, roots/run directories are private and checked for reparse points, reads use `O_NOFOLLOW` where available and bounded regular-file checks, and deletion here uses the literal `context.json`. |
| 12, 13, 14 | `sonder_runtime/adapters/debugging/launcher.py`, 105 -> 105 (two annotations); 100 -> 100; entire file identical | Not actionable under the same root/name controls. The write uses an unpredictable exclusive temporary filename, mode 0600, and `O_NOFOLLOW` where available before replacement of the allowed destination. |
| 15, 16, 17, 18 | `assetgen.py`, 392 -> 392; 415 -> 415; 452 -> 452; 485 -> 485; entire file identical | Needs source trace. These are requested SVG, brief-document, and HTML artifacts derived from the supplied brief; SVG/HTML text is escaped. Escaping is not secret redaction. No runtime credential source into the brief was established. |
| 19 | `curriculum_run.py`, 56 -> 56; entire file identical | Not actionable for the reviewed normal outcome path. `record_self_graded_outcome` returns signal, reward, generated interaction/lesson IDs, and status prose; it does not return lesson content or credentials. |
| 20 | `game_forge.py`, 429 -> 429; entire file identical | Needs source trace. `save_source` writes the caller-supplied generated game source to its requested project. No runtime credential addition is present at this sink. |
| 21, 22 | `game_ladder.py`, 170 -> 170; 318 -> 318; entire file identical | Needs source trace. The writes retain generated source for compilation and the requested game artifact. Child environments use the canonical secret-removal policy; that control does not itself prove the source text lacks secrets. |
| 23 | `scripts/nightly_selfmod.py`, 1803 -> 1803 | Needs source trace. The changed lines in this file are documentation only. Settings printed here contain mode, enabled, and retention values; the generic logger also receives lifecycle results and objective prose, whose reported secret source is unavailable. |
| 24 | `scripts/selfmod_forever.py`, 68 -> 68; entire file identical | Needs source trace. A generic lifecycle logger prints normal status and bounded exception text. No concrete credential-bearing source is identified by the available annotation. |
| 25, 26, 27, 28 | `sonder_runtime/__main__.py`, 98 -> 98; 105 -> 105; 108 -> 108; 115 -> 115 | Needs source trace. These are shared CLI output renderers. Config/diagnostic callers use `as_redacted_dict`; key rotation explicitly omits the new key. Other callers emit operational reports, so the inaccessible password source must be checked before a complete dismissal. The change here exports only the default workspace-root setting. |
| 29, 30 | `sonder_runtime/adapters/debugging/launcher.py`, 81 -> 81; 83 -> 83; entire file identical | Not actionable under the trusted private-root policy. These are symlink/reparse checks on paths constrained by the run-root and name controls above. They are protective metadata checks, not an unguarded caller-selected file write. |
| 31, 32 | `sonder_runtime/interfaces/http/serve.py`, 4752 -> 4751; 4767 -> 4766 | Not actionable for a remote caller with supported operator configuration. Response origins require exact membership in the configured origin allowlist. Observatory additionally requires a GET/OPTIONS telemetry route. Remote header data cannot independently choose a different echoed value. |

Nineteen annotations have a reviewed defeating control/predicate; fourteen
retain the source-trace qualification. No unrelated behavior change follows
from this note. The aggregate alerts remain visible for a maintainer with
Code Scanning access to inspect the complete flows and assess any remaining
privacy risk. A passing test suite would not replace that missing evidence.
