# codegen-arena-shooter — a worked example, and the run that produced the guards

This is the harness that drove Sonder's local ensemble to write an 8-file C#
arena shooter (menu, class select, lobby, UDP networking, 3D team deathmatch).
It is kept because **every guard in `codegen_loop.py` exists because this run
was measured doing the wrong thing**, and a tool whose safety rules have no
worked example is a tool whose rules get removed by the next person who finds
them inconvenient.

`codegen_build_loop` (the MCP tool) is the productised, language-agnostic form.
This is the specific driver: C#/dotnet, with the per-file contracts in
`specs.py`.

## Running it

```bash
python build_with_sonder.py --tiers code,reasoning --sequential --attempts 2
python build_with_sonder.py --only GameMap.cs      # one file
python build_with_sonder.py --repair-only          # skip generation
python build_with_sonder.py --resume               # skip files already on disk
```

Targets `FpsGame_Sonder/` beside it. Needs the .NET SDK and a running Ollama.

## What it measures

`specs.py` holds one contract per file, written as **transformation** prompts —
the exact Raylib signatures, the exact field names, the exact algorithm — never
**recall** prompts. That distinction is measured, not stylistic: the local model
is strong when every fact it needs is in the prompt and weak when it must
remember an API.

`apiextract.py` reads the *actual* public API back out of already-generated
files, so a dependent file is told what its dependencies really expose rather
than what someone intended them to. Note `codegen_loop.extract_api` is the
generic, shallower version of the same idea; this one understands C# well enough
to distinguish a field from an auto-property, which decides whether a caller may
assign to it.

## The findings, in the order they cost something

Each of these was a real run producing a wrong result, not a hypothetical:

| Failure | Measurement | Guard |
|---|---|---|
| Repair deletes instead of fixing | 44% of a file returned to insert two `.`; 13% to add one `int` | `SHRINK_FLOOR` rejects a repair below 75% |
| Regeneration is not monotonic | rewriting a clean `ClassKit.cs` produced 6 new errors incl. `System.Drawing`'s `Color.FromArgb` | a clean file is never regenerated; the incumbent is the baseline |
| Per-file scoring picks the wrong winner | one rewrite went 10→14 on itself while taking the project 106→14 | score on **total** project errors |
| Imports are pure bookkeeping | fixing them by table took a project 50→2 in one pass | deterministic, never asked of the model |
| Character-level blindness | shown the broken line *and* the exact compiler error, the model echoed it back verbatim, twice | known slips rewritten from a table |
| Cross-file contracts do not hold | the contract was in every prompt and files still disagreed: 28 member-not-found, 16 assign-to-readonly | dependents get the API extracted from source |
| Namespace drift | one file of eight declared `namespace ArenaShooter`, hiding its types from the other seven | mechanical dedent to the majority |

## The one that kept recurring: a count is not a total

A compiler runs **parse → declare → bind**. An error in an early phase means the
count says how far it *got*, not how broken the project *is*. This was misread
four times in one session:

| Reported | Real | Cause |
|---|---|---|
| 2 | 99 | parse errors; the binder never ran |
| 1 | 109 | `CS0111` duplicate member — parsed fine, could not be *declared*, so no dependent bound |
| 1 | 101 | ditto, after a "fix" |
| 0 | unknown | build killed by timeout before printing anything — rendered as BUILD SUCCEEDED |

And then the detector built to catch it became the next bug. It matched the
`CS1xxx`/`CS8xxx` **code range**, and ranges leak both ways:

- `CS8180`, `CS8124` are the parser but sit outside `CS1xxx` → missed.
- `CS1061`, `CS1503`, `CS1501` are **binder** errors inside it → falsely
  flagged. On a build with *zero* real parse errors: 101 errors, 36 claimed as
  parse errors.

The false positives are the damaging half — they pin the "masked" bit at 1 for
every candidate, collapsing `score = (masked?, total)` back into ranking by
total, the exact bug the tuple existed to prevent. The loop then discarded a
generated file that had genuinely fixed the parse error, keeping one whose
masked "1" was really 101.

**Match message shape, never a code range.** Shape also generalises across
toolchains; a range never does.

## Outcome

The model never reached a compiling build. Final honest state: **99 errors, zero
parse-shaped** — a fully bound build, and the first count in the exercise that
was a total rather than a floor.

Per file: Screens 32, Program 24, MatchState 18, Combatant 11, LobbyNet 5,
NetProtocol 5, GameMap 4.

What it got right: every algorithm it was handed a contract for — the
axis-at-a-time wall slide, the ray/sphere intersection, all 24 class-stat values
transcribed exactly. What it got wrong was everything *between* files, plus
reaching for a recalled API over the supplied one (Unity's `Vector3.normalized`
with `System.Numerics` signatures in the prompt).

**It writes functions; it does not hold a system.** Feeding each file the real
extracted API of its dependencies did not fix that — `Combatant.cs` was handed
`GameMap`'s actual signatures and still called four members that exist in
neither the contract nor the extraction.
