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
| `SONDER_NUM_GPU` | Optional layer-placement override. Leave unset for Ollama capability detection; `0` forces CPU-only. |
| `SONDER_KEEP_ALIVE` | How long a model stays resident, e.g. `30m` on a roomy host, shorter if RAM-tight. |

### About `SONDER_NUM_GPU`

Leave it unset initially. Ollama detects the usable CPU and any supported
Apple Metal, AMD, Intel, NVIDIA, or other accelerator backend on the live host.
Use `status` to inspect residency and benchmark the actual workload before
pinning an integer. `0` is an explicit CPU-only choice; a large positive value
requests aggressive layer offload but is not a portable default and can create
memory pressure on small or shared accelerators.

## Then add the guidance

Registration gives your agent the tools. It does not tell it which work to
send. Append [CLAUDE.sonder.md](CLAUDE.sonder.md) to your `~/.claude/CLAUDE.md`
so every session starts knowing the difference between transformation and
recall.
