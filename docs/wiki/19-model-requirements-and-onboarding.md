# Model Requirements & Onboarding

What you actually have to install before Sonder is useful, how to verify it,
how to choose what a request runs on, and exactly what happens when a
configured model is not there.

This page is the task-shaped companion to two reference pages: read
[Model Tiers & Gateway](08-model-tiers-and-gateway.md) for how tiers, lanes,
and the gateway work, and the [Model Catalog](18-model-catalog.md) for which
model families and sizes suit your hardware.

## 1. The short answer

**Ollama plus one generative local model, exposed as the `sonder:latest`
alias.** That is the whole hard requirement for local chat, code work, the
REPL, the OpenAI-compatible API, and MCP.

Everything else in the catalog is optional and is needed only when you turn on
the matching capability:

| Capability | Needs | Required to start? |
|---|---|---|
| REPL / API / MCP chat and code work | one generative local model as `sonder:latest` | **Yes** |
| Semantic memory, lesson recall, vector search | an embedding model (default `nomic-embed-text`) | No |
| Hard multi-step reasoning routing | a model bound to the `reasoning` tier | No |
| Image / screenshot analysis | a vision model bound to the `vision` tier | No |
| Hosted `cloud-*` tiers | no local download; explicit cloud opt-in | No |

The base tiers `fast`, `code`, and `general` all default to the same
`sonder:latest` alias
([`rules.DEFAULT_MODELS`](../../sonder_runtime/domain/runtime_policy/rules.py)),
so one model covers all three until you decide otherwise. `reasoning` and
`vision` ship **unbound** — an unbound tier is not offered anywhere and the
capability router degrades that work to a base tier.

> Nothing about model presence blocks the process from starting. Startup
> preflight probes Ollama and counts the catalog; `/ready` requires Ollama to
> be *reachable*, not any particular model. An empty catalog fails on the first
> chat turn, not at boot — which is why the verification step below is worth
> thirty seconds.

## 2. Bootstrap and run

### Bundled install

```powershell
# Windows PowerShell, from the extracted bundle
.\bootstrap-engine.cmd
.\sonder.cmd
```

```bash
# Linux/macOS, from the extracted bundle
./bootstrap-engine.sh
. ./sonder-runtime.sh
"$SONDER_PYTHON" ./sonder_repl.py
```

`bootstrap-engine` detects host RAM/VRAM, picks a base model size, starts
Ollama if it is not already running, and then runs `setup_alias.py`.

### From source

```bash
git clone https://github.com/Krilliac/Sonder-runtime.git
cd Sonder-runtime
python3 -m venv venv
./venv/bin/pip install -r requirements-runtime.txt
./venv/bin/python setup_alias.py          # pull base model + create sonder:latest
./venv/bin/python -m sonder_runtime repl
```

On Windows PowerShell use `.\venv\Scripts\python.exe` /
`.\venv\Scripts\pip.exe` in place of the POSIX paths.

### What `setup_alias.py` actually does

1. Resolves a base model: `--model`, else `SONDER_BASE_MODEL`, else a choice
   from live host memory (`qwen2.5-coder` at `1.5b` / `3b` / `7b` for
   `<4 GB` / `4–8 GB` / `≥8 GB` RAM).
2. Pulls it if it is not already in the local Ollama catalog (skipped under
   `--offline`).
3. Tries to pull the embedding model (`--embed-model`, default
   `nomic-embed-text`). **This step is allowed to fail**: it prints a note
   telling you recall/lessons need an embedding model, and the install
   continues.
4. Creates the `sonder:latest` alias from the base model with Sonder's system
   prompt and `temperature 0.2`.

Useful variants:

```bash
python setup_alias.py --no-embedding          # smallest chat-only install
python setup_alias.py --offline               # never contact a registry
python setup_alias.py --gguf /path/model.gguf # import a portable GGUF
python setup_alias.py --from-usb              # discover a GGUF on removable media
```

## 3. Verify what is installed

All of these are read-only.

```bash
ollama list                                   # the provider's own catalog
ollama show sonder:latest                     # prove the alias exists
python -m sonder_runtime doctor               # config, policy, Ollama reachability
python -m sonder_runtime preflight            # startup checks without binding a port
python -m sonder_runtime diagnostics          # redacted diagnostic bundle
```

`doctor` and `preflight` both probe `GET /api/tags` on the configured Ollama
URL and report the host plus how many models are installed; `doctor` warns when
Ollama answers with an empty catalog, because that is the state where every
chat turn is about to fail. Add `--skip-ollama` to either when you deliberately
want an offline check.

From inside the REPL:

```text
/model            # active tier, every selectable tier, and installed tags
/runtime          # shared policy: tier -> model, lane -> tier, readiness
```

`/runtime` (alias `/models`) prints the readiness projection that matters most
on a new machine:

```text
  readiness:
    local chat/code: ready
    semantic memory: ready (nomic-embed-text)
    reasoning: not configured (optional)
    vision: not configured (optional)
```

A tier whose model is configured but absent from the live catalog is listed
under `WARNING missing local model(s)` and turns its readiness line into
`requires <model>`.

Over the HTTP adapter (bearer key required) and MCP:

```bash
curl -s http://127.0.0.1:11435/live                 # process alive
curl -s http://127.0.0.1:11435/ready                # 503 until Ollama is reachable
curl -s -H "Authorization: Bearer $SONDER_API_KEY" \
  http://127.0.0.1:11435/v1/models                  # tiers this server will accept
```

MCP clients have the same read-only surfaces as tools: `status` (installed
models and current VRAM residency), `runtime_policy_status`, `diagnostics`,
and `environment_status`.

## 4. Select a model or a tier

Three distinct scopes, from narrowest to broadest.

**Per session, in the REPL** — `/model` changes only this REPL session:

```text
/model                      # list tiers and installed tags
/model general              # switch to a tier's live binding
/model qwen2.5-coder:7b     # pin this session to an exact installed tag
```

`/model` refuses a tag that is not in the live catalog (with a near-miss
suggestion), refuses a tier that is configured but currently withheld — a
`cloud-*` tier without cloud opt-in, for example — and refuses to change
anything at all if Ollama did not answer, rather than pinning a target that
would fail on the next turn.

**Shared runtime policy** — `/runtime set` is a guarded edit of the
hot-reloadable `runtime_policy.json` that every surface reads:

```text
/runtime set fast=<model> code=<model> general=<model>
/runtime set reasoning=<model> vision=<model>     # empty value leaves one unset
/runtime set embedding=<installed-embedding-model>
/runtime set router=fast workbench=code autopilot=code fleet=code review=general
/runtime reset                                    # back to built-in defaults
```

Only installed local models are accepted, an embedding binding must positively
declare the embedding capability, execution lanes may pin to `fast`, `code`, or
`general` only, and the policy can never name a cloud model or widen a
permission.

**Per request** — an API caller sets `"model"` to a tier name (`sonder`,
`code`, `general`, `cloud-code`, …) or to an exact installed tag; the
capability router may upgrade a request to a specialist tier you have actually
bound, and never to one you have not.

> `SONDER_FAST`, `SONDER_CODE`, `SONDER_GENERAL`, `SONDER_REASONING`,
> `SONDER_VISION`, and `SONDER_EMBED_MODEL` **seed the policy file the first
> time it is created**. Once `runtime_policy.json` exists, the stored policy
> wins and separately launched surfaces cannot drift with their inherited
> environment. Change bindings with `/runtime set` after that, or
> `/runtime reset` to return to built-in defaults.

## 5. When a configured model is absent

Sonder degrades in named, inspectable ways rather than failing at startup.

| Situation | What actually happens |
|---|---|
| Ollama not running | `/ready` returns 503 naming the `ollama` dependency; the runtime re-probes every 15 s and recovers with no restart. `doctor`/`preflight` report the endpoint failure. |
| Ollama reachable, catalog empty | Startup succeeds. `doctor` warns that no models are installed. The first chat turn fails at the provider. Fix with `setup_alias.py`. |
| `sonder:latest` missing, default settings | The local learning route falls back to `SONDER_CODE_LOCAL`, which itself defaults to `sonder:latest` — so unless you pointed it at an installed tag, the turn still fails at Ollama. Re-run `setup_alias.py`, or set `SONDER_CODE_LOCAL` to an installed model. |
| `sonder:latest` missing, `SONDER_STRICT=1` | The call is refused up front with a configuration error naming the alias and telling you to run `setup_alias.py` — no silent substitution. |
| A base tier (`fast`/`code`/`general`) bound to a model that is not installed | `/runtime` lists it under `missing_models` and its readiness line reads `local chat/code: requires <tier>=<model>`. Rebind with `/runtime set`. |
| An optional tier (`reasoning`/`vision`) left unset | Not an error. The tier is dropped from `TIERS`, disappears from `/v1/models` and `/model`, and the capability router degrades that work to a base tier. |
| An optional tier bound to a missing model | Readiness reads `<tier>: requires <model>`; other tiers are unaffected. |
| A vision feature invoked with no `vision` binding | Refused with `vision model is not configured; run /runtime set vision=<installed-vision-model>`. |
| Embedding model missing or not embedding-capable | Embedding calls soft-fail to `None`: semantic recall and vector search stop contributing, lexical (FTS5) retrieval and core chat keep working. Readiness reads `semantic memory: requires embedding model <model>`. |
| A `cloud-*` tier without cloud opt-in | Refused before anything leaves the machine: `hosted/cloud tiers are disabled. Set SONDER_ALLOW_CLOUD=1 …`. `/model` and `/v1/models` do not offer the tier at all. |

## 6. Recommended optional capabilities

In the order most installations want them:

1. **Embedding model** — `nomic-embed-text` (or BGE/E5/mxbai). Bootstrap
   already tries to install it. Without it you keep chat and lexical retrieval
   but lose semantic recall and lessons, which is most of what makes the
   runtime improve over time.
2. **A distinct `code` model** — a coder-specialized model on the `code` tier
   while `fast` stays small is the highest-value split on a machine with room
   for two resident models.
3. **`reasoning`** — a reasoning/"thinking" model for proofs, derivations, and
   design work. Worth it around 24 GB VRAM, where a 32B fits.
4. **`vision`** — a vision-language model when you want local screenshot and
   image analysis. `vision_analyze` requires a loopback Ollama endpoint and a
   model that explicitly declares the `vision` capability.
5. **Hosted `cloud-*` tiers** — no local download, but see the consent gates
   below.

Sizing guidance per VRAM band lives in the
[Model Catalog](18-model-catalog.md); the step-by-step build-out is the
[assemble-model-collection runbook](../runbooks/assemble-model-collection.md).

> **Installed is not integrated.** A tag in `ollama list` only means the
> provider has it. Sonder must also expose a bounded integration for that
> capability, and you must bind the model to it. A downloaded speech or
> reranker tag does not switch on transcription or reranking.

## 7. Local by default; remote and cloud are separate opt-ins

Two independent consent gates, neither on by default:

- **Remote Ollama** — pointing `OLLAMA_HOST` (or `[ollama].url`) at a
  non-loopback host is rejected unless `SONDER_ALLOW_REMOTE_OLLAMA=1` /
  `[ollama].allow_remote = true`. A remote endpoint must additionally use
  `https`, because prompts and embeddings would otherwise cross the network in
  the clear. This is still *your* Ollama — it changes where inference runs, not
  which vendor sees it.
- **Hosted cloud tiers** — `cloud-code` and `cloud-general` run on Ollama's
  servers. They are withheld from every selection surface until
  `SONDER_ALLOW_CLOUD=1` / `[features].cloud = true`, and prompts sent to them
  leave this machine.

Cloud tiers need no local weights, so they are never a startup requirement.
They answer without local-lesson injection, while grounded good outcomes are
still captured locally as lessons the local model can retrieve later. Memory,
capture, and distillation always stay on the runtime host.

## 8. Quick troubleshooting

| Symptom | First command | Likely fix |
|---|---|---|
| `/ready` is 503 | `python -m sonder_runtime doctor` | Start Ollama; the runtime recovers on its own. |
| Chat errors mention a model name | `ollama list` | The tag is not installed — `setup_alias.py`, or `/runtime set` to an installed one. |
| "alias not found" | `ollama show sonder:latest` | Re-run `setup_alias.py`. |
| Recall/lessons never fire | `/runtime` | Bind an embedding model; then `/embeddings apply` to refresh stored vectors. |
| A tier is missing from `/model` | `/runtime` | It is unbound (optional tier) or withheld (cloud without opt-in). |
| Answers are weak, not broken | `/model` | Model capability, not configuration — see [Model Catalog](18-model-catalog.md). |
