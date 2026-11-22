# Registering Sonder with Claude Code

Assumes you have already cloned Sonder and created its venv (see the
[Quickstart](../../README.md#quick-start)). Below, `<SONDER_DIR>` is the
absolute path to your checkout. This is the per-client detail page; the
all-clients overview (MCP, console, app, HTTP, SDK) is
[connect-clients.md](../../docs/runbooks/connect-clients.md).

The supported entry point is `python -m sonder_runtime mcp`. Registering
`server.py` directly is the legacy path; see "Options and other notes" in
[connect-clients.md](../../docs/runbooks/connect-clients.md#mcp-clients-stdio).

`sonder_runtime` is not installed into the venv (the repo has no packaging
metadata), so the server must be started with `PYTHONPATH=<SONDER_DIR>`.
MCP clients start it in their own project directory, not the checkout, and
without `PYTHONPATH` it fails with `No module named sonder_runtime`.

## The easy way

```bash
claude mcp add sonder --scope user -e PYTHONPATH=<SONDER_DIR> -- <SONDER_DIR>/venv/bin/python -m sonder_runtime mcp
```

On Windows the interpreter lives at `<SONDER_DIR>\venv\Scripts\python.exe`.

Verify:

```bash
claude mcp list
```

## By hand

Add a `sonder` entry under `mcpServers` in `~/.claude.json`:

```json
{
  "mcpServers": {
    "sonder": {
      "type": "stdio",
      "command": "<SONDER_DIR>/venv/bin/python",
      "args": ["-m", "sonder_runtime", "mcp"],
      "env": {
        "PYTHONPATH": "<SONDER_DIR>",
        "SONDER_ALLOW_CLOUD": "0"
      }
    }
  }
}
```

`PYTHONPATH` is required (the checkout is not an installed package).
`SONDER_ALLOW_CLOUD` is optional; omit it and cloud tiers stay off. The block
is strict JSON with no comments, so it can be pasted as is.

Windows paths need escaped backslashes in JSON:
`"<SONDER_DIR>\\venv\\Scripts\\python.exe"`, and likewise for the `PYTHONPATH` value.

Use the venv interpreter, not a system `python`. A system interpreter will
appear to work and then skip optional-dependency code paths.

## Environment worth setting

None of these are required; the defaults are safe and local. Values in
examples are examples, not defaults. Defaults checked 2026-11-20.

| Variable | Effect |
|---|---|
| `SONDER_ALLOW_CLOUD` | `1` permits the metered `cloud-*` tiers. Default off; leave unset or `0` to keep everything on your machine. |
| `SONDER_LEARN_TIERS` | Which tiers participate in the lesson loop, e.g. `fast,code,general`. Default: all local tiers (`DEFAULT_LEARN_TIERS` in `server.py`). |
| `SONDER_FAST` / `SONDER_CODE` / `SONDER_GENERAL` | Pin a base tier to a specific Ollama model. Default `sonder:latest` for each (`sonder_runtime/adapters/data/model_lists.json`). |
| `SONDER_REASONING` / `SONDER_VISION` | Default empty (unbound). Pin the specialist tiers the capability router prefers for proofs and image work. `none` leaves one unbound, and that work falls back to a base tier. |
| `SONDER_NUM_GPU` | Optional layer-placement override. Leave unset for Ollama capability detection; `0` forces CPU-only. |
| `SONDER_KEEP_ALIVE` | How long a model stays resident, e.g. `30m` on a roomy host, shorter if RAM-tight. Default `2m` (`KEEP_ALIVE` in `server.py`). |

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
