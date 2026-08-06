# Agent integrations

Sonder is most useful when the agent driving it knows *what it is good at*.
Registering the MCP server is a five-minute job; knowing which work to send it
is the part that took measurement. Both are here.

```
integrations/
├── claude/
│   ├── CLAUDE.sonder.md      guidance block for ~/.claude/CLAUDE.md
│   └── mcp-config.md         registering the server with Claude Code
├── codex/
│   ├── AGENTS.sonder.md      guidance block for ~/.codex/AGENTS.md
│   └── config.toml.example   [mcp_servers.sonder] block
└── IMPORT_PROMPT.md          paste this at your agent and it sets itself up
```

## Three ways to install

**1. Ask your agent to do it.** Open [IMPORT_PROMPT.md](IMPORT_PROMPT.md),
copy the block, paste it into a Claude Code or Codex session. It reads these
files, registers the server against your own paths, and appends the guidance
to your config. Read the diff it proposes before you accept it — that is the
same advice this integration gives about Sonder's own output.

**2. Copy the two files by hand.** Register the MCP server per
[claude/mcp-config.md](claude/mcp-config.md) or
[codex/config.toml.example](codex/config.toml.example), then append
`claude/CLAUDE.sonder.md` to your `~/.claude/CLAUDE.md`, or
`codex/AGENTS.sonder.md` to your `~/.codex/AGENTS.md`.

**3. Take the guidance only.** If you drive Sonder through the REPL, the HTTP
API, or the app rather than MCP, the offload guidance still applies — it is
about the model, not the transport. Read `claude/CLAUDE.sonder.md` and ignore
the registration steps.

## The one thing to read if you read nothing else

Send it **transformation**, not **recall**.

If every fact the task needs is already in your prompt — restructure this,
mirror that, implement this fully specified function — a local 7B does it
well. If the model has to supply a fact you did not give it, it will answer
confidently and be wrong. A lookup table is the trap: it looks like the most
mechanical request imaginable and is pure recall.

Measured over seven judged offloads: transformation 4/4 usable, recall 3/3
wrong. Details and the failure cases are in the guidance files.

## Keeping these current

These files describe behaviour that was measured, not assumed. If you find
the guidance is wrong on your hardware or your model mix, that is worth an
issue or a PR — see [CONTRIBUTING.md](../CONTRIBUTING.md). Include what you
ran and what you got; a number beats an impression.
