# Research port: bounded instruction discovery

Date: 2026-08-21  
Branch: `agent/port-research-findings`

## Finding

Zero documents project/personal instruction discovery for `AGENTS.md`,
`ZERO.md`, and `.zero/AGENTS.md`, with bounded discovery rather than an
unrestricted recursive scan. Easy Agent places context management and skills
alongside the orchestration loop.

Sources: <https://github.com/gitlawb/zero> and
<https://github.com/ConardLi/easy-agent>

## Implemented slice

Sonder now has `InstructionRegistry`, which:

- scans only explicitly supplied roots and known instruction filenames;
- applies deterministic low-to-high source precedence;
- bounds file count and UTF-8 byte size;
- rejects symlink roots/files and malformed reads;
- exposes content plus SHA-256 provenance records.

Evidence: `tests/test_instruction_discovery.py`.
