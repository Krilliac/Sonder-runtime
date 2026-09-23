# Live agent context roots

The `[context]` configuration section supplies read-only roots for the live
agent prefix producer. Each root list is bounded to 16 entries and must contain
absolute, non-link paths. These roots control instruction and skill discovery;
they do not expand model-writable workspace roots or project authority.

```toml
[context]
instruction_bundled_roots = ["D:/Sonder-runtime/bundled"]
instruction_global_roots = ["C:/ProgramData/Sonder/instructions"]
instruction_configured_roots = []
skill_bundled_roots = ["D:/Sonder-runtime/skills"]
skill_global_roots = ["C:/ProgramData/Sonder/skills"]
skill_configured_roots = []
```

Discovery precedence is bundled, global, project, then configured, with later
sources replacing an earlier record of the same logical name. The producer
scans only bounded known instruction files and skill manifests, preserves the
last complete snapshot when a refresh becomes partial, and marks the request
incomplete when no usable snapshot exists. Dynamic turns, retrieval, and memory
remain outside the reusable prefix.

The runtime composition root creates `LiveAgentContextProducer` from the typed
configuration before constructing `AgentLaneService`. Missing roots are safe
and contribute no records; malformed relative or link roots fail configuration
validation rather than silently widening discovery.
