# local-llm — GPU offload bridge for Claude Code

Lets Claude offload subtasks to a local model on your **RTX 4050 (6 GB VRAM)** via Ollama,
so it can save its own budget on bulk/mechanical work. Models load into VRAM **only when
called** and unload after a short idle (`keep_alive`), so the GPU stays free otherwise.

## Pieces
- **Ollama** (CUDA) — model runtime + API at `http://127.0.0.1:11434`. Auto-starts via the
  tray app; manual start: `ollama serve`.
- **MCP server** `local-llm` — `server.py` (runs in `./venv`), registered at user scope in
  `~/.claude.json`. Exposes tools Claude can call:
  - `offload(prompt, tier="fast", system="", temperature=0.2, num_predict=1024)`
  - `status()` — what's installed / what's in VRAM right now
  - `unload(tier="all")` — free VRAM immediately

## Tiers — escalation ladder, cheapest first
**Local** (private, free, offline; one in VRAM at a time on the 6 GB 4050):
| tier | model | use for |
|------|-------|---------|
| `fast` | qwen2.5:3b | summaries, classification, simple text, quick answers |
| `code` | qwen2.5-coder:7b | code generation, refactor snippets, code explanation |
| `general` | qwen2.5:7b-instruct | harder text reasoning, longer drafts, rewriting |

**Cloud** (Ollama-hosted via your `trilobite` sign-in — frontier-size, **metered**, and the
**prompt leaves this machine**; no local VRAM used):
| tier | model | use for |
|------|-------|---------|
| `cloud-code` | qwen3-coder:480b-cloud | hard/large coding beyond the local 7B |
| `cloud-general` | gpt-oss:120b-cloud | hard reasoning / large-context text |

> Claude is instructed to prefer **local** tiers for your private repos (mangos / Cambrian /
> MMORPG) and only reach for `cloud-*` when the task truly needs it or you ask.

## Using it
1. **Restart Claude Code** so the `local-llm` tools load (MCP tools are read at session start).
2. Ask things like *"summarize this log with the local model"* or *"use the local coder to
   draft this function"* — or just let Claude decide to offload.
3. Use it directly too: `ollama run qwen2.5-coder:7b`, or any OpenAI-compatible client
   pointed at `http://127.0.0.1:11434/v1`.

## Tuning (env vars, optional)
- `LOCAL_LLM_KEEP_ALIVE` (default `2m`) — how long a model lingers in VRAM after use.
- `LOCAL_LLM_FAST` / `LOCAL_LLM_CODE` / `LOCAL_LLM_GENERAL` — swap the model per tier.
- `LOCAL_LLM_TIMEOUT` (default `300` s).

## Swapping models
`ollama pull <model>` then point a tier env var at it. Good 6 GB-friendly options:
`llama3.2:3b`, `qwen2.5:7b`, `deepseek-coder-v2:16b` (won't fully fit — partial offload).

## Trilobite — self-learning coding loop

`trilobite` is a self-improving local coding model built on top of this bridge.
The learning lives in this MCP server (retrieval + reflection + reward); Ollama
only serves frozen weights.

- **Interactive use:** call `mcp__local-llm__trilobite("...")` — or just say
  "use trilobite". The reply ends with `[interaction_id: <id>]`.
- **Close the loop:** once you know how the answer did, call
  `record_outcome(<id>, "tests_passed" | "accepted" | "compiled" | "rejected" | "failed")`.
  Good outcomes distill a retrievable lesson that improves future answers.
- **Fleets:** the normal `offload(...)` tool learns automatically (`learn=True`);
  pass `learn=False` to skip capture. Cloud tiers never learn (privacy).
- **Setup (one-time):** `./venv/Scripts/python.exe setup_alias.py` (pulls
  `nomic-embed-text`, creates the `trilobite` Ollama alias).
- **State:** `memory.db` (gitignored) at the server root holds interactions,
  outcomes, and lessons.
