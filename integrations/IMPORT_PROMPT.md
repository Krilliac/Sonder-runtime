# Let your agent install it

Copy everything in the block below and paste it into a Claude Code or Codex
session. It is written so the agent discovers your paths rather than assuming
them, and so it shows you the changes before making them.

Read what it proposes before you accept. That is the same thing this
integration tells the agent about Sonder's own output, and it applies here
too.

---

```text
Set up Sonder Runtime for me as a local-model offload lane.

Source of truth (read these first, do not rely on memory of them):
  https://github.com/Krilliac/Sonder-runtime/blob/main/integrations/README.md
  https://github.com/Krilliac/Sonder-runtime/blob/main/integrations/claude/CLAUDE.sonder.md
  https://github.com/Krilliac/Sonder-runtime/blob/main/integrations/claude/mcp-config.md
  https://github.com/Krilliac/Sonder-runtime/blob/main/integrations/codex/AGENTS.sonder.md
  https://github.com/Krilliac/Sonder-runtime/blob/main/integrations/codex/config.toml.example

Do this:

1. Work out whether Sonder is already on this machine. Look for a clone
   (a directory containing server.py and contribute.py) and check whether an
   MCP server named "sonder" is already registered. Tell me what you found
   before changing anything. If there is no clone, stop and tell me — I will
   clone it and re-run this.

2. Check the prerequisites and report which are missing rather than
   installing anything unasked: Ollama reachable on 127.0.0.1:11434, at least
   one pulled chat model, an embedding model, and a venv inside the checkout
   with its dependencies installed.

3. Register the MCP server for whichever agent you are, using the venv
   interpreter inside the checkout and absolute paths for this machine. Not a
   system python. Show me the exact config change first.

4. Append the guidance file to my agent config — CLAUDE.sonder.md to
   ~/.claude/CLAUDE.md if you are Claude Code, AGENTS.sonder.md to
   ~/.codex/AGENTS.md if you are Codex. Append; do not overwrite, and do not
   reorder or drop anything already in that file. If a Sonder section is
   already present, show me a diff instead of duplicating it.

5. Default to private: do not enable the cloud tiers. Leave SONDER_NUM_GPU
   unset so Ollama detects this host's CPU/Metal/AMD/Intel/NVIDIA backend.
   Pin a numeric override only after measuring it. Let Sonder detect CPU thread
   count unless this machine has an operator-chosen limit.

6. Verify rather than assume. Restart or reload so the server is picked up,
   then call status and diagnostics and show me the output. If a tier points
   at a model I have not pulled, say so and name it — do not pull gigabytes
   without asking.

7. Summarise: what you changed, what you did not, anything that needs my
   decision, and one sentence on which work you will send to Sonder from now
   on and which you will not.

Constraints: make no change outside the MCP registration and the guidance
append. Do not modify my other instructions. Do not send any of my code to a
cloud tier. If a step fails, stop and tell me — do not work around it.
```

---

## If you would rather not paste a wall of text

The short version, for an agent that already has web access:

```text
Read https://github.com/Krilliac/Sonder-runtime/blob/main/integrations/README.md
and set Sonder up for me. Show me every config change before you make it,
keep cloud tiers off, and verify with status when you are done.
```
