---
name: sonder-security-and-privacy
description: >-
  The Sonder Runtime security model and privacy boundaries a maintainer must never
  weaken, with the enforcing mechanism behind each so a diff touching them can be
  judged correctly. TRIGGER when the user asks "is this safe to expose", "security
  posture", "privacy boundary", "what leaves the machine", "enable remote access",
  "unsafe lab", "does this weaken security", or "rotate the API key". DO NOT
  TRIGGER for config-variable mechanics, precedence, or how a flag is parsed (use
  sonder-config-and-flags), for commit/PR gating (use sonder-change-control), or
  for the history of reverted features (use sonder-failure-archaeology).
---

# Sonder security and privacy boundaries

Sonder Runtime is a local-first AI runtime that, by design, executes code, edits
files, fetches the network, runs unattended, and can modify its own source. The
trust boundary is therefore **the process itself**: anything that can send it a
request can in principle ask for any of those. Treat access to a Sonder endpoint
as equivalent to shell access on the host (`SECURITY.md`).

Two framing rules govern everything below:

1. **Model output is not a security boundary.** Guarantees are host-enforced;
   an uncensored model changes what it will *discuss*, never what the runtime
   will *let it do* (`docs/wiki/09-security-model.md`).
2. **Every boundary below has a named mechanism.** When reviewing a diff, find
   the mechanism, not just the docstring. If a change moves a check, the check
   must still fire on every path that reached it before.

Report vulnerabilities **privately** via the GitHub Security tab ("Report a
vulnerability" on `github.com/Krilliac/Sonder-runtime`), never as a public
issue. In scope: sandbox escapes from file/execution tools, auth bypass on the
served API, credential/memory leakage into logs, lessons, or exports, privilege
escalation through the elevation broker. Out of scope: "the tools execute code
when asked", attackers who already hold the API key or a local shell, and base
model output.

## When NOT to use this skill

- How `SONDER_*` variables are parsed, their precedence, or config file layout
  → `sonder-config-and-flags`. This skill covers what the gates *protect* and
  why they must stay default-off; that one covers the plumbing.
- Whether a change may be committed, which CI gates apply → `sonder-change-control`.
- Why a past capability (e.g. `shell_run`) was removed → `sonder-failure-archaeology`.

## 1. Network posture ladder (never skip a rung)

| Posture | Mechanism | Rules |
| --- | --- | --- |
| **Loopback (default, recommended)** | Server binds `127.0.0.1`; Ollama loopback | Nothing exposed. Evaluators stay here. |
| **Local dev service** (`deploy_sonder.sh --serve`) | Loopback-only systemd unit; generates a random `SONDER_API_KEY` into `/etc/sonder/sonder-local.env` mode `0600` | Not a remote deployment. **Never port-forward it.** Older revisions of this script bound `0.0.0.0` with a plaintext URL — upgrade, remove any firewall rule exposing port 11435, and rotate the key if it ever crossed a network in cleartext. |
| **Server-private profile** (`docs/runbooks/install-server-private.md`) | Runtime listener stays `127.0.0.1` behind a TLS reverse proxy (`docs/runbooks/secure-remote-access.md`, `packaging/reverse-proxy/nginx-sonder.conf`) | Never expose the runtime port directly: a valid credential grants the full tool surface. |

The load-bearing invariant: a **non-loopback bind is rejected before the socket
opens** unless *both* `tls_terminated_by_proxy = true` and a
`SONDER_API_KEY` of at least `MIN_API_KEY_LENGTH = 24` characters are set
(`sonder_runtime/platform/config.py`, `_is_loopback_host` + validation around
line 480). There is no override; the reference topology keeps loopback even
then. Auth is a bearer key compared in constant time, with a per-peer
auth-failure token bucket and `AUTH_FAILED` audit events.

```bash
# See the validation with your own eyes:
grep -n "non-loopback" sonder_runtime/platform/config.py
```

## 2. Consent gates — default-off, never enable-able by runtime policy

Each gate is an independent opt-in. Defaults live in `FeaturesConfig` /
`OllamaConfig` (`sonder_runtime/platform/config.py`); the bootstrap
(`sonder_runtime/__main__.py`) exports the resolved value into the environment
at startup. Runtime policy, model output, HTTP, and MCP can never turn one on.

| Gate | What crossing it means | Enforcing code |
| --- | --- | --- |
| `SONDER_ALLOW_CLOUD` | Prompt text leaves the machine to a hosted model | `features.cloud`, default `False` |
| `SONDER_WEB_TOOLS` | Runtime performs outbound HTTP | `features.web = False` default; adapters raise `web tools disabled by SONDER_WEB_TOOLS` (`sonder_runtime/adapters/web_fetch.py`, `web_search.py`, `weather.py`, `location.py`, `artifact_fetch.py`) |
| `SONDER_ALLOW_REMOTE_OLLAMA` | Prompts **and embeddings** leave to a remote Ollama | `ollama.allow_remote = False`; config loader rejects a non-loopback `[ollama].url` without it, and non-loopback also demands `https` |
| `SONDER_LOCATION_CONSENT` | Approximate IP geolocation lookup | `features.location_consent` |
| `SONDER_EXPOSE_REASONING` | Model reasoning channel becomes visible | `features.expose_reasoning` |
| `SONDER_ALLOW_PRIVATE_COT` | Private chain-of-thought served | `features.allow_private_cot` **plus** an explicit `allow` rule for `admin_private_chain_of_thought` in `permissions.json` — the env var alone is never enough; the built-in rule denies it and the tool reads that rule itself |
| `SONDER_PROCESS_INSPECTION=enabled:bounded-read-only` | Process inventory / memory-risk scanning | exact-string opt-in; anything else is off (`sonder_runtime/adapters/process_risk.py`) |

Review trap (verified 2026-08-22): the legacy root module `web_tools.py:176`
falls back to `"1"` when `SONDER_WEB_TOOLS` is **unset in the environment** —
safe only because the runtime bootstrap always exports `"0"`/`"1"` from config
before adapters run (`sonder_runtime/__main__.py:534`). A diff that lets an
adapter consult the env var before bootstrap, or imports the root module
standalone, silently flips the default open. Flag it.

## 3. Guarded-tool boundaries and their mechanisms

Never weaken any row without an issue first (capability/security changes are
tier-3 in change control). Sources: `SECURITY.md` mitigation list,
`sonder_runtime/adapters/filesystem/file_ops.py`, `docs/wiki/09-security-model.md`.

### File surface (`file_ops.py` — the numbers are load-bearing)

- Roots: all file tools operate inside configured roots (`SONDER_FILE_ROOTS` /
  `file_roots.local`); escaping takes explicit `extra_roots` or a bypass token.
  Path canonicalization blocks traversal; `_require_no_reparse_components`
  rejects any reparse point (Windows symlink/junction) in the path.
- Ceilings: `MAX_READ_BYTES = 256_000`, `MAX_WRITE_BYTES = 1_000_000`,
  `MAX_TRANSFER_BYTES = 64 MiB`, `MAX_BATCH_FILES = 32`.
- Secrets never read/written: `SECRET_FILES` (`.credentials.json`, `.netrc`,
  `.token`, `auth.json`, `credentials.json`, `secrets.json`, `token.json`) and
  `SECRET_SUFFIXES` (`.key`, `.p12`, `.pem`, `.pfx`).
- Sensitive directories never traversed for reads:
  `SENSITIVE_READ_DIRECTORIES = {.git, .ssh, .aws, .azure, .kube}`.
- Control-plane config protected: `CONTROL_CONFIG_FILES` includes
  `permissions.json`, `file_roots.local`, `workflows.json`.
- Personal corpus protected (`_is_personal_corpus`): `combined_personal.jsonl`
  and any `sonder-personal-lora/` tree are read-sensitive — raw personal
  conversation/training data.
- `file_delete`: **dry-run by default**; a real delete requires the exact
  confirmation string the tool itself returned, root restrictions, and a
  developer-authorization check. `_require_safe_recursive_delete` refuses
  trees containing symlinks/reparse points or protected paths, and refuses
  deleting a configured root.
- `file_batch_write`: never targets secrets/control state, never traverses
  symlinks, requires explicit create-vs-overwrite intent, rolls back completed
  writes on later failure when restoration remains possible.

### Read-only and transform tools (each fails closed)

| Tool | Mechanism |
| --- | --- |
| `data_query` | SQLite via **read-only URI + deny-by-default authorizer**; JSON/JSONL/CSV/TSV use exact structured filters, no expression evaluation |
| `data_convert` | no-follow opened handle, rejects sensitive/control/reparse paths, atomically creates a **new** destination only — never overwrites |
| `repo_log` / `repo_show` / `repo_blame` | read-only, project-bound, **argv-only**; pagers, external diffs, textconv, and parent-repository discovery disabled; gitfile targets must stay in authorized roots |
| `archive_list` / `archive_extract` | ZIP/TAR only; every member prevalidated under entry/byte/**ratio**/depth/result/time ceilings; absolute or traversal paths, links, devices, encrypted ZIPs, collisions, **nested archives**, and existing destinations rejected before a staged non-overwriting extraction is promoted |
| `archive_create` | prevalidates inputs and directory membership, refuses sensitive paths/links/special files/overwrites, revalidates mutations, atomically publishes a staged archive |
| `text_patch` | narrow unified-diff grammar, explicit relative UTF-8 text files only; **preview is the default**; apply prevalidates the whole patch, rejects deletes/renames/binary/sensitive/link targets, digest-guarded rollback that will not overwrite a concurrent change |
| `workspace_compare` | metadata and SHA-256 digests only, never content; fails closed on sensitive state, reparse points, special files, identity races, or any ceiling |
| `log_inspect` | fixed host parsers only, under hard file/byte/line/result/output/time ceilings; validated already-open regular-file handle |
| `artifact_risk_inspect` | **never renders or executes input**, never returns raw content; static indicators are evidence, not a verdict; incomplete or unsupported scans are **never labelled clean** |
| `local_service_probe` | direct explicit MCP operation only — excluded from agents, read-only sessions, loops, and autopilot, because loopback response bodies can hold host-local secrets; DNS pinned to exclusively-loopback answers, rechecked before connect |

### Execution surface

- `script_run` applies `SONDER_EXECUTION_RISK_POLICY` (default `report`;
  operators may set `deny-high` / `deny-medium` / `deny-unknown`). A caller may
  **strengthen but never weaken** the configured policy. Enforcing `deny-*`
  modes currently **fail closed for every launch** because a portable exact
  inspected-handle-to-interpreter handoff is not yet available — this is
  deliberate (avoids a pathname-swap TOCTOU bypass) and labelled defense in
  depth, **not an OS sandbox**. Do not "fix" deny modes by launching from the
  inspected pathname.
- Process inspection (Windows-only): one exact PID, read/query rights only,
  returns fixed indicator names/counts and aggregate accounting — never command
  lines, module paths, addresses, strings, or memory bytes. No debug
  privilege, no suspend/write/inject.
- Process execution generally is argv-only with bounded timeout/output — a
  containment layer, not a sandbox.

## 4. Network egress and git hardening

`web_tools.py` (root module, transport for web fetch):

- **DNS-pinned connections**: the address that passed policy checks is the
  address the TCP socket connects to (`_PinnedHTTPConnection` /
  `_PinnedHTTPSConnection`), closing the resolve→connect TOCTOU (time-of-check
  vs time-of-use) gap.
- Targets must be **globally routable** (`_is_globally_routable`: `is_global`
  and not private/loopback) — no fetching the host's own services by name.
- Ceilings: `MAX_RESPONSE_BYTES = 512_000`, `MAX_DECOMPRESSED_BYTES =
  2_000_000` (zip-bomb guard on the decompressor itself), `MAX_REDIRECTS = 5`,
  `MAX_DNS_ADDRESSES = 16`.

`git_tools.py` (read-only history + runtime self-update):

- Environment sanitized: everything `GIT_*` and `SSH_ASKPASS` stripped, then
  `GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`, `GIT_OPTIONAL_LOCKS=0` pinned —
  ambient git env can redirect the repo/config/index or inject config.
- Checkout filters neutralized: every configured `filter.*.clean/smudge/process`
  gets a passthrough override (`_FILTER_COMMAND_RE`), because filters are
  config-driven command injection.
- All output and process lifetime bounded; stdin is `DEVNULL`.
- Runtime self-update fast-forwards only when `origin` normalizes into
  `_TRUSTED_RUNTIME_ORIGINS` (frozenset of the three canonical
  `Krilliac/Sonder-runtime` URL forms) and the checkout is clean.

## 5. Privacy pipeline — what is stored where, what may leave

| Store / artifact | Contents | Sharing rule |
| --- | --- | --- |
| `memory.db` | Raw conversations, facts, lessons, embeddings (`memory_store.py`) | **Never commit or share.** Gitignored (`memory.db*`). |
| `operations.db` | Identifiers, counts, hashes, durations, **redacted** paths only | Never prompts, memory text, workspace contents, or credentials. |
| Distilled lessons | Text that passed the privacy classifier | Only these are candidates for export. |
| `contrib/lessons_contrib.jsonl` | Classifier-scrubbed opt-in export | Written locally by `python contribute.py`; nothing uploads automatically. **Read the file before attaching it anywhere.** |
| `personal_dataset.jsonl`, `combined_personal.jsonl`, `sonder-personal-lora/`, `sonder-personal-merged/`, `Modelfile.personal` | Personal training data and adapters | Never commit — all gitignored. **Weights memorize training data**; a shared adapter is a shared corpus. |

Mechanisms:

- **20-rule privacy classifier** (`PRIVATE_RULES` in `contribute.py`, exactly
  20 entries, verified): Windows/Unix/home/workspace/UNC paths, file URIs,
  emails, credential assignments, sensitive and authorization headers, known
  credential formats (AWS `AKIA…`, `github_pat_…`, `sk-…`, `xox…`, etc.),
  URL-embedded credentials, private-key blocks, long hex/base64/urlsafe
  tokens, JWTs. Lessons matching any rule are excluded from storage-for-export;
  `privacy_preview` returns typed substitution markers like `<credential>`,
  never the matched value. `export_training_data.py` and `export_lessons.py` reuse the
  same rules.
- **Redaction before logging** (`sonder_runtime/platform/logging.py`): bearer
  tokens, API keys, known secret env values, URL credentials, private-key
  blocks, and configured workspace path prefixes stripped. A redaction failure
  replaces the whole detail with the literal string `[REDACTION_FAILED]` and
  increments a metric — it degrades observability, **never privacy**. A diff
  that catches that failure and logs the original value anyway is a leak.
- **Fleet provenance redaction** (`fleet_provenance.py`): evidence paths under
  `.git`, `.ssh`, `.aws`, `.azure`, `sonder-personal-lora`, filenames
  `memory.db`, `permissions.json`, `credentials.json`, `secrets.json`,
  `combined_personal.jsonl`, and suffixes `.key/.pem/.p12/.pfx` are treated as
  sensitive and withheld.
- **Scrubbed child environments**: model-authored subprocess boundaries
  (repair campaigns, selfmod validation/git calls; see
  `sonder_logging.child_environment` usage in `git_tools.py`) remove secret-,
  credential-, token-, session-, approval-, and bypass-like variables. This
  reduces accidental credential inheritance; it does not make a hostile
  process safe.
- Recall is project-scoped; cross-project recall requires an explicit override.
- Cleanup path exists: `/privacy`, `/privacyfix`, and `memory_privacy_repair`
  (dry-run first) for lessons already flagged.

## 6. Unsafe lab mode — document the gate, not a how-to

A deliberately dangerous model-evaluation override for **disposable isolated
environments only** (`docs/runbooks/unsafe-lab.md`). It is not an OS sandbox
and provides no containment. What matters for review is the *gate*:

- Activation requires `SONDER_UNSAFE_LAB_ACK` to **exactly equal** a specific
  132-character acknowledgement sentence (`ACKNOWLEDGEMENT` in
  `sonder_runtime/platform/unsafe_lab_policy.py`). Boolean/truthy,
  abbreviated, case-changed, or whitespace-modified values **fail closed**.
- Even with the exact sentence, activation is refused when `SONDER_HOST` is
  not loopback, when the process is root/elevated, when `SONDER_ALLOW_CLOUD`
  is truthy, or when `OLLAMA_HOST` is malformed or non-loopback
  (`validation_error`, fail-closed on any error).
- Activation appends a durable JSONL warning to
  `$SONDER_HOME/audit/unsafe-lab.jsonl` (path overridable only via
  `SONDER_UNSAFE_LAB_AUDIT_PATH`); status/diagnostics display the mode.
- Blast radius: it removes the local model-loop host-tool allowlist, project
  scoping, read-only gate, web/location gate, and file-approval gate. The
  shared file-approval bypass also reaches the **46 direct MCP call paths**
  that consult it — direct MCP is inside the blast radius, not an unchanged
  boundary. Remaining time/output bounds are reliability controls, not
  security.
- What it does **not** change: the artifact-execution risk policy, the exact
  process-inspection opt-in, and the hosted-agent restriction on local-only /
  host-data tools all remain enforced.

Related startup-capability rule (`docs/adr/ADR-003-startup-capabilities.md`):
the `--unrestricted-tools` / `--unrestricted-selfmod` capability booleans are
parsed once at bootstrap, frozen (`RuntimeCapabilities(frozen=True)`), and are
**never placed in `OperationContext`** — so they cannot be toggled or forged
by HTTP, MCP, model output, agents, or runtime config mutation. Any diff that
threads a capability boolean through a request context violates this ADR.

## 7. Operational security

- **Suspected secret exposure** (`docs/runbooks/suspected-secret-exposure.md`):
  treat plausible exposure as real; contain by rotating the key with a
  **minimal** overlap window (seconds, not the default day), then restart the
  service. The `rotate-key` command syntax is owned by
  `sonder-run-and-operate`; the runbook has the full containment sequence.
- **Planned rotation** (`docs/runbooks/rotate-credentials.md`): same CLI with
  the default 24 h overlap. Why the design is safe: the new key is written
  into the secrets file mode 0600 and never printed; the previous key's
  **hash** (never plaintext) is stored with a mandatory expiry so both keys
  work until it lapses. If account auth is in use, also rotate
  `SONDER_AUTH_SECRET` (invalidates all tokens).
- **Update trust**: releases are accepted only through a TUF (The Update
  Framework) signed metadata chain — `tools/tuf_repo.py` publisher with
  **2-of-3 thresholds for root and targets**, hash verification,
  rollback/freeze protection, adversarially-safe archive extraction. The TUF
  dependency pins live in `requirements-update.txt`; **a version bump there
  requires re-running the adversarial acceptance suite**
  (`tests/production/test_tuf_publisher.py` plus the
  archive-safety/rollback/freeze tests) before shipping.
- **Release integrity**: the `build-apps` workflow publishes
  `sonder-runtime-sbom.cdx.json` (SBOM, software bill of materials) and
  `sonder-runtime-provenance.intoto.json` (in-toto provenance) alongside
  release artifacts (`.github/workflows/build-apps.yml`).

## 8. Review checklist for any diff touching this surface

Ask each question; one "yes" without an issue and explicit maintainer intent
is a blocker:

1. Does it **widen a root** or add a path that escapes `SONDER_FILE_ROOTS`,
   `SECRET_FILES`, `SECRET_SUFFIXES`, `SENSITIVE_READ_DIRECTORIES`,
   `CONTROL_CONFIG_FILES`, or the personal-corpus guard?
2. Does it add an **env override or runtime-policy path that can enable a
   consent gate**, or read a gate before bootstrap seals it (the
   `web_tools.py` fallback trap in §2)?
3. Does it **silence a redaction failure** or log/store the original value
   when redaction returns `[REDACTION_FAILED]`?
4. Does it convert **fail-closed to fail-open** — e.g. make `deny-*` execution
   modes launch, label an incomplete artifact scan clean, let unsafe-lab
   activation proceed on a validation exception, or accept a near-miss
   acknowledgement string?
5. Does it raise a ceiling (`MAX_*` in `file_ops.py` / `web_tools.py`) or
   remove a prevalidation step (archive ratio/depth, patch digest guard,
   reparse rejection) without a stated attack analysis?
6. Does it **advertise a tool that a gate later denies**, or expose
   `local_service_probe` to agents/loops/autopilot?
7. Does it put a capability boolean into `OperationContext`, or make one
   mutable after startup (ADR-003)?
8. Does it weaken subprocess hygiene — reintroduce inherited `GIT_*` env,
   re-enable checkout filters, pass shell strings instead of argv, or stop
   scrubbing credential-like vars from model-authored children?
9. Does it commit or export anything from the never-share list in §5?

Historical anchor: the `shell_run` tool was **wholesale reverted** for safety
(commit `ae9503b0`, "Revert feat: shell_run tool + opt-in test scaffolds").
Capability-expanding surfaces that go wrong get reverted, not patched — see
`sonder-failure-archaeology` for the full story and siblings.

Honest scope (do not oversell in docs or reviews): Sonder protects a single
owner's private runtime. It is not multi-tenant authorization, not a code
sandbox, and not a defense against a compromised host or a malicious Ollama
binary. The guardrails raise the bar for an over-eager model and an exposed
endpoint; they are not OS-level isolation. Multi-user enforcement remains
`open` (identity seam exists, defaults to one owner).

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). Sources: `SECURITY.md`,
`docs/wiki/09-security-model.md`, `sonder_runtime/adapters/filesystem/file_ops.py`,
`sonder_runtime/platform/config.py`, `sonder_runtime/platform/unsafe_lab_policy.py`,
`web_tools.py`, `git_tools.py`, `fleet_provenance.py`, `contribute.py`,
`docs/adr/ADR-003-startup-capabilities.md`, `docs/runbooks/{unsafe-lab,secure-remote-access,install-server-private,rotate-credentials,suspected-secret-exposure}.md`,
`deploy_sonder.sh`, `requirements-update.txt`, `.github/workflows/build-apps.yml`.

Re-verify volatile facts (run from repo root):

```bash
# File-surface ceilings and secret/sensitive sets
grep -n "MAX_READ_BYTES\|MAX_WRITE_BYTES\|MAX_TRANSFER_BYTES\|MAX_BATCH_FILES\|SECRET_SUFFIXES\|SENSITIVE_READ_DIRECTORIES" sonder_runtime/adapters/filesystem/file_ops.py | head
# Egress ceilings and routability check
grep -n "MAX_RESPONSE_BYTES\|MAX_DECOMPRESSED_BYTES\|MAX_REDIRECTS\|MAX_DNS_ADDRESSES\|_is_globally_routable" web_tools.py | head
# Non-loopback bind rejection + minimum key length
grep -n "MIN_API_KEY_LENGTH\|non-loopback" sonder_runtime/platform/config.py
# Consent-gate defaults (all should be False)
grep -n "web: bool\|cloud\|allow_remote: bool\|allow_private_cot\|location_consent" sonder_runtime/platform/config.py | head
# Unsafe-lab acknowledgement still exact-match and fail-closed
grep -n "ACKNOWLEDGEMENT\|refuses\|fail" sonder_runtime/platform/unsafe_lab_policy.py | head
# Privacy classifier still has exactly 20 rules
python -c "import contribute; print(len(contribute.PRIVATE_RULES))"
# Never-commit list still gitignored
grep -n "memory.db\|personal" .gitignore
# Trusted self-update origins
grep -n -A4 "_TRUSTED_RUNTIME_ORIGINS" git_tools.py | head -6
# shell_run revert anchor still resolves
git log --oneline -1 ae9503b0
```
