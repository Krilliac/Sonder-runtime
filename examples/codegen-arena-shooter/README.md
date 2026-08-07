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
than what someone intended them to. `codegen_loop.extract_api` is the generic,
shallower version of the same idea; this one understands C# well enough to
distinguish a field from an auto-property, which decides whether a caller may
assign to it.

## The findings, in the order they cost something

Each was a real run producing a wrong result, not a hypothetical:

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
count says how far it *got*, not how broken the project *is*. Misread four times
in one session:

| Reported | Real | Cause |
|---|---|---|
| 2 | 99 | parse errors; the binder never ran |
| 1 | 109 | `CS0111` duplicate member — parsed fine, could not be *declared*, so no dependent bound |
| 1 | 101 | ditto, after a "fix" |
| 0 | unknown | build killed by timeout before printing anything — rendered as BUILD SUCCEEDED |

**Then the detector built to catch it became the next bug.** It matched the
`CS1xxx`/`CS8xxx` **code range**, and ranges leak both ways:

- `CS8180`, `CS8124` are the parser but sit outside `CS1xxx` → **missed**.
- `CS1061`, `CS1503`, `CS1501` are **binder** errors inside it → **falsely
  flagged**. On a build with zero real parse errors: 101 errors, 36 claimed as
  parse errors.

The false positives are the damaging half — they pin the "masked" bit at 1 for
every candidate, collapsing `score = (masked?, total)` back into ranking by
total, the exact bug the tuple existed to prevent. The loop then discarded a
generated file that had genuinely fixed the parse error, keeping one whose
masked "1" was really 101.

**Match message shape, never a code range.** Shape also generalises across
toolchains; a range never does.

## Result

Once the detector was honest, the baseline was **99 errors, zero parse-shaped**
— a fully bound build, and the first count in the exercise that was a total
rather than a floor. The loop then ran properly: **99 → 85**, still zero
parse-shaped, verified independently against `dotnet build`.

Per file: Screens 32, Program 22, MatchState 16, NetProtocol 5, LobbyNet 5,
Combatant 4, GameMap 1.

Where the 14 came from, across 7 files and 12 regeneration attempts:

| Outcome | Count |
|---|---|
| Regeneration kept (a genuine improvement) | **2** — GameMap 4→1, Combatant 11→4 |
| Regeneration rejected as parse-broken | **7** |
| Regeneration rejected as worse | 2 |
| File already clean, skipped | 1 |

Every rejected candidate reported a *lower* number than the incumbent — 1, 2, 2,
2, 6, 14, 21 against totals of 85–105 — because each collapsed at the parser
before the compiler could count the rest. **The model reliably produces files
that fail earlier and therefore look better.** A loop scoring on raw totals
climbs exactly that gradient, which is why the masked tier must rank on blockers
rather than count.

The deterministic fixers did steady, unglamorous work throughout: wrong-library
calls rewritten, `System.Numerics`/`System.Net` imports added, and the
`namespace ArenaShooter` wrapper stripped from three separate files that each
re-added it.

## The honest capability read

It never reached a compiling build. What it got right was every algorithm it was
handed a contract for — the axis-at-a-time wall slide, the ray/sphere
intersection, all 24 class-stat values transcribed exactly. What it got wrong
was everything *between* files, plus reaching for a recalled API over the
supplied one (Unity's `Vector3.normalized` with `System.Numerics` signatures in
the prompt).

**It writes functions; it does not hold a system.** Feeding each file the real
extracted API of its dependencies did not fix that — `Combatant.cs` was handed
`GameMap`'s actual signatures and still called four members that exist in
neither the contract nor the extraction.

---

# v2: the harness owns the declarations

Everything above is the whole-file loop. Its final error mix said what to do
next: CS1061 (28), CS0103 (21), CS0272 (16), CS0117 (15), CS1503 (13) — **every
dominant class is two files disagreeing about an API**, not bad code inside a
method. Handing each file the *real extracted* surface of its dependencies did
not fix it. That is a capability limit, not a prompting one, so v2 stops asking.

`skeleton.py` owns every declaration. `bodynotes.py` holds one algorithm note
per body. `build_skeleton.py` fills one body at a time. Three consequences:

* **The baseline is 0 errors, not 85.** The skeleton compiles before any model
  output exists, so every later error belongs to exactly one body.
* **The v1 failure mode is structurally impossible.** 7 of 12 regenerations
  were rejected as parse-broken there; here the model never writes the
  structure, and a bad body reverts to its placeholder alone.
* **The metric stops being an error count**, which would sit near zero by
  construction and say nothing, and becomes *bodies implemented / N*.

## Result: 17 of 38 bodies, build clean

86 minutes, `code=sonder:latest` + `reasoning=deepseek-r1:7b`. Verified
independently against `dotnet build`: **Build succeeded, 21 `NotImplementedException`
stubs remaining.**

| File | Bodies kept |
|---|---|
| `ClassKit.cs` | 0 / 1 |
| `GameMap.cs` | 5 / 6 |
| `Combatant.cs` | 3 / 3 |
| `NetProtocol.cs` | 2 / 4 |
| `MatchState.cs` | 2 / 5 |
| `LobbyNet.cs` | 1 / 5 |
| `Screens.cs` | 1 / 7 |
| `Program.cs` | 3 / 7 |

Of the 21 reverts, 15 raised the error count and 6 produced *masking* errors —
so even confined to a single body, output that breaks the parser is still a
sixth of attempts.

**This is not "85 errors → 0".** Those are different metrics and comparing them
would be the same mistake this directory exists to document. The honest claim:
v1 never produced a compiling project at all, and could not say how much of it
the model actually got right; v2 produces one that compiles and states exactly
how much is real — 17 bodies — and exactly how much is a stub.

## The finding: 89% vs 17%, split by what the body needs

| Body kind | Files | Kept |
|---|---|---|
| **Pure algorithm** — every fact in the prompt | `GameMap`, `Combatant` | **8 / 9 (89%)** |
| **Library API** — must recall a real API correctly | `Screens` (Raylib), `LobbyNet` (UdpClient) | **2 / 12 (17%)** |

A **5.2x** gap on one run, and it is the transformation-versus-recall line drawn
exactly. `MoveWithSlide`'s axis-at-a-time resolution, the ray march, the
Yaw/Pitch forward vector — all correct. Anything needing `Raylib.DrawRectangle`
or `UdpClient.Receive` used the way those libraries actually work — mostly not.

The practical rule: **give a local model bodies whose every fact is in the
prompt, and write the API-bound ones yourself.** Splitting a project along that
line is worth more than any amount of extra prompting.

## Compiling is still not correct

`GameMap.IsWallAt` was kept — it compiles. It was told to convert the point to
cell indices and call `IsWallCell`. Instead it re-derived the wall test by
copying the constructor's border/pillar arithmetic, ignoring the `_walls` array
entirely. Same answer for this map; silently wrong the moment the map changes,
and now a second source of truth.

That is [SpecBench](https://arxiv.org/abs/2605.21384)'s gap in miniature —
passing the visible check while deviating from the specification — and it is
why *bodies kept* is a measure of what compiled, not of what is right.
