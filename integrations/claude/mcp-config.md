# Registering Sonder with Claude Code

Assumes you have already cloned Sonder and created its venv (see the
[Quickstart](../../README.md#quickstart)). Below, `<SONDER_DIR>` is the
absolute path to your checkout.

## The easy way

```bash
claude mcp add sonder --scope user -- <SONDER_DIR>/venv/bin/python <SONDER_DIR>/server.py
```

On Windows the interpreter lives at `<SONDER_DIR>\venv\Scripts\python.exe`.

Verify:

```bash
claude mcp list
```

## By hand

Add a `sonder` entry under `mcpServers` in `~/.claude.json`:

```jsonc
{
  "mcpServers": {
    "sonder": {
      "type": "stdio",
      "command": "<SONDER_DIR>/venv/bin/python",
      "args": ["<SONDER_DIR>/server.py"],
      "env": {
        // Optional. Omit this and cloud tiers stay off.
        "SONDER_ALLOW_CLOUD": "0"
      }
    }
  }
}
```

Windows paths need escaped backslashes in JSON:
`"C:\\Users\\you\\sonder-runtime\\venv\\Scripts\\python.exe"`.

Use the venv interpreter, not a system `python`. A system interpreter will
appear to work and then skip optional-dependency code paths.

## Environment worth setting

None of these are required; the defaults are safe and local.

| Variable | Effect |
|---|---|
| `SONDER_ALLOW_CLOUD` | `1` permits the metered `cloud-*` tiers. Leave unset or `0` to keep everything on your machine. |
| `SONDER_LEARN_TIERS` | Which tiers participate in the lesson loop, e.g. `fast,code,general`. |
| `SONDER_FAST` / `SONDER_CODE` / `SONDER_GENERAL` | Pin a base tier to a specific Ollama model. |
| `SONDER_REASONING` / `SONDER_VISION` | Pin the specialist tiers the capability router prefers for proofs and image work. `none` leaves one unbound, and that work falls back to a base tier. |
| `SONDER_NUM_GPU` | Layers to offload to GPU. **`999` means "all of them"** and is what you want if the model fits in VRAM — see the note below. |
| `SONDER_KEEP_ALIVE` | How long a model stays resident, e.g. `30m` on a GPU box, shorter if RAM-tight. |

### About `SONDER_NUM_GPU`

Set it to `999` unless you have a reason not to. Ollama's default offload
heuristic is conservative: on a 6 GB card it will leave a chunk of a 7B on the
CPU and cost you roughly a third of your throughput, while reporting nothing
unusual. Forcing all layers, on that same card:

```
qwen2.5-coder:7b @ 32k ctx   num_gpu default -> ~24 tok/s, ~30% on CPU
qwen2.5-coder:7b @ 32k ctx   num_gpu=999     -> 36.4 tok/s, 100% GPU
```

If the model genuinely does not fit, Ollama falls back rather than failing, so
this is a safe default. `status` shows current residency, and if you benchmark
Sonder yourself, replicate its options — measuring the raw Ollama API without
`num_gpu` measures a configuration Sonder never uses.

## Then add the guidance

Registration gives your agent the tools. It does not tell it which work to
send. Append [CLAUDE.sonder.md](CLAUDE.sonder.md) to your `~/.claude/CLAUDE.md`
so every session starts knowing the difference between transformation and
recall.
