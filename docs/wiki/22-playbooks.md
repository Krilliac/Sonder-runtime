# Playbooks

Playbooks are curated topic notes in `<SONDER_HOME>/playbooks/`. Facts remain
short durable statements in SQLite; lessons remain atomic takeaways distilled
from grounded successful interactions. Playbooks can record a failed approach
and its fix, a complete procedure, or a measured result with its conditions.
They are ordinary UTF-8 Markdown that the owner can inspect, edit and diff.

The agent has two tools:

```text
playbook_note(topic, category, title, body, evidence, triggers?)
playbook_read(topic)
```

Write one specific idea at a time. Include exact commands, measurements, dates,
and evidence. Reuse an existing topic such as `shells`, `builds`, `environment`,
or `benchmarks`. Exact duplicates are refused; similar entries and topic names
are flagged for review. Admission uses existing lexical write-quality and FTS
retrieval machinery, without asking a model or creating embeddings.

## Categories and entry format

| Category | What to record |
|---|---|
| `pitfall` | Symptom, root cause, fix, and evidence |
| `procedure` | One task and its ordered steps |
| `environment` | Machine, network, or toolchain facts with an observation date |
| `measurement` | Numbers, method, date, and operating conditions |
| `decision` | Choice, reason, and alternatives rejected |
| `tool-guide` | How to drive a tool, flags, and gotchas |
| `preference` | The owner's stated working preferences |

Each entry has an id, date, category, title, body, evidence, provenance, taint
label, status, and optional `supersedes` id. Status is `proposed`, `approved`,
`rejected`, or `superseded`. Provenance names session/request/model/repository
information when available; unknown information is not invented.

Each topic is `<slug>.md`. The compact `index.md` uses one line per topic:

A row contains a Markdown link from the topic title to its `.md` filename,
then `— <category> — open when: <comma-separated trigger phrases>`. For
example, Shells links to `shells.md` with category `pitfall` and triggers
`powershell, shell quoting`.

Keep the generated entry boundaries and metadata labels when editing. Text
outside a managed entry belongs to the owner and is preserved. Malformed or
ambiguous entries do not become prompt instructions. The runtime rereads topic
files, serializes writes with a shared lock, and uses atomic file replacement;
it refuses a detected intervening owner edit instead of overwriting it.

## Approval and owner review

The default is **approval required**. Proposed notes stay on disk for review
and are excluded from both the approved index and loaded topic context.

```console
python -m sonder_runtime playbooks list
python -m sonder_runtime playbooks show shells
python -m sonder_runtime playbooks approve shells ENTRY_ID
python -m sonder_runtime playbooks reject shells ENTRY_ID
python -m sonder_runtime playbooks edit shells ENTRY_ID --body "Replacement text"
python -m sonder_runtime playbooks rm shells ENTRY_ID
```

The REPL exposes the same workflow through `/playbooks`. Proposed notes also
use the existing pending-call approval ledger. Approval is bound to the entry
content: an approval for an older version cannot approve changed text. An
explicit reload refreshes the index for subsequent turns. Restarting or
opening a new session also takes a fresh index snapshot.

For the existing inbox workflow, use `/approvals`, then `/approve CALL_ID`,
then `/playbooks reload` to apply matching grants and refresh that session's
index. `/playbooks list` also applies matching grants. The owner management
command is conservatively classified with approval operations; the note tool
has the same `ask` risk as `sonder_remember_fact`, and reading is `safe`.

Approval modes:

- `required`: every new entry is proposed.
- `owner_corrections_auto`: a trusted, direct owner correction in the current
  turn can be approved automatically. The REPL's explicit
  `/playbooks correction <topic> <category> <title> --body <text>` path supplies
  this evidence. Merely claiming to quote the owner in a model argument does
  not establish it.
- `auto`: trusted host-authored notes can be approved automatically.

All modes require owner approval for untrusted source material. Public MCP
and HTTP tools cannot submit a trust or owner-correction flag; absent trusted
host provenance, model-authored proposals are conservatively tainted. Notes
copied from web pages, documents, tool outputs or other agents cannot promote
themselves by including approval metadata in their body.

## Reading and prompt stability

The approved index is placed with stable system content before request-specific
instructions. It is cached by session, with a bounded cache, and changes only
on a new session or explicit reload. Topic bodies are volatile context.
Matching uses token-aware lexical trigger phrases, opens only matched topics,
and selects approved entries by lexical relevance. There are no model calls,
embedding calls or network requests in the matching path.

Loaded notes are explicitly framed as owner reference data, below system
policy and current owner instructions. Hosted model paths and hosted account
tools do not receive these local owner notes. With no directory or no approved
entries, the playbook fragments are empty and existing prompts are unchanged.

## Configuration

Use the runtime's ordinary `sonder.toml` configuration:

```toml
[playbooks]
approval = "required"
categories = ["pitfall", "procedure", "environment", "measurement", "decision", "tool-guide", "preference"]
max_entry_bytes = 8192
max_topic_bytes = 131072
max_index_bytes = 4096
max_topics = 64
max_topics_per_turn = 3
max_context_bytes = 12288
environment_stale_days = 30
measurement_stale_days = 90
```

Categories are configurable slug names. Limits are validated at startup;
index size cannot exceed 4 KiB, and volatile context cannot exceed 32 KiB.
The final framed topic context counts toward its UTF-8 byte budget. Oversized
entries are skipped on retrieval rather than truncating a procedure midway.

## Safety, maintenance and measurement

The durable capture redactor removes known configured secret values and
credential patterns before persistence. Owner-only file permissions use the
runtime's platform helpers where supported. Playbooks perform local file and
SQLite operations only, and refuse unsafe paths and link-based redirection.
Manually pasted secrets should be removed by the owner; the runtime never
silently rewrites unrelated owner text to sanitize it.

`memory_quality_report` adds a report-only playbook section when playbooks
exist. It reports status counts, approved index bytes, exact duplicate merge
plans, stale environment/measurement entries, and conflict candidates using
the existing lesson conflict detector. Lexical candidates are distinguished
from pairs backed by outcome evidence. The existing lesson repair job does
not delete or rewrite playbooks. Promotion of lessons is not automatic.
Owners can inspect `python -m sonder_runtime playbooks merge TOPIC` and repeat
with `--apply` to mark exact duplicates superseded; the original text remains.

Loaded topic and entry ids can be linked to a captured interaction in the
existing memory database. The report joins the canonical outcome rows,
preserving caller-versus-machine provenance, and retains at most 10,000 usage
rows. These are correlations with later outcomes, not proof that the agent
followed a note or that the note caused success. Agent turns without a legacy
interaction id contribute process-local load counters, without fabricated
outcome attribution.

See [Memory & Learning](06-memory-and-learning.md) for the existing fact,
lesson, and outcome contracts.
