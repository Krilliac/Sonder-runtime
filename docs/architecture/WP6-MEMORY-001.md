# WP6-MEMORY-001 — Typed memory and procedural promotion

The WP6 memory slice adds pure typed records for procedural, factual,
episodic, and preference memories. Evidence is attributable and weighted;
contradictions are first-class records rather than silently reducing a score.

`ProceduralLearningService` is a read-only application policy. It promotes
only procedural memories with sufficient evidence, rejects stale or
contradictory records by default, normalizes duplicate content, and enforces
content and candidate-count bounds. It does not persist or publish a lesson;
those side effects remain behind the existing memory ports.

Focused verification: `tests/test_wp6_typed_memory.py`.

