# Sonder Games

Two C# 3D shooters live here, built for different reasons. Read this before
assuming which one is which.

| Folder | Who wrote the code | Status |
|---|---|---|
| `FpsGame/` | Claude, by hand | Builds, runs, 35/35 headless checks pass |
| `FpsGame_Sonder/` | Sonder's local model ensemble | See "The capability test" below |

Both target .NET 10 and [Raylib-cs](https://www.nuget.org/packages/Raylib-cs) 8.0.
Raylib-cs is the only third-party dependency.

---

## FpsGame — the working reference

A single-player 3D FPS: mouse-look, WASD with wall sliding, hitscan shooting,
chasing enemies, medkits, an exit objective, and a HUD.

```bash
cd "D:/Sonder Games/FpsGame"
dotnet run -c Release              # play it
dotnet run -c Release -- --smoke   # headless verification, exit 0 = all checks pass
```

**Every asset is generated in code.** Nothing was downloaded and no binary art
is stored. `Assets.cs` paints five textures pixel by pixel and synthesizes five
sounds sample by sample, writing real `.png` and `.wav` files into
`bin/Release/net10.0/assets/` on first run:

- Textures: offset-course brick walls, riveted floor plating, lit ceiling
  panels, an enemy sprite on a transparent background, a medkit.
- Audio: hand-written 16-bit mono RIFF/WAV — a noise-burst gunshot with a low
  thump, an impact tick, a descending death tone, a two-step pickup chime, and
  a hurt buzz.

The generator uses a fixed-seed PRNG, so the same build always produces
byte-identical assets: a texture change is a code change.

`--smoke` exists because "it compiled" proves nothing about a game and this
machine builds far more often than it can open a window. It verifies map
integrity, that nothing spawns inside a wall, collision and wall-sliding, ray
casts, the ray/sphere hit test, combat arithmetic, ammo rules, and that the
enemy AI actually closes distance and lands hits without clipping through
geometry.

### Controls
WASD move · Shift sprint · Mouse look · LMB fire · R restart · Esc quit

---

## FpsGame_Sonder — the capability test

This one exists to answer a question honestly: **can a 7B-class local model,
running entirely on this machine, write a working game?**

`build_with_sonder.py` is the harness. It does not write game code. It:

1. Puts one tightly-scoped contract per file to Sonder's model ensemble
   (`ensemble_answer(..., mode="code")`), which asks two local models
   independently and has a third pass pick-and-patch the best result.
2. Writes what comes back to `FpsGame_Sonder/`.
3. Runs the real compiler.
4. Feeds the **actual compiler errors** back for repair rounds.

```bash
cd "D:/Sonder Games"
python build_with_sonder.py --tiers code,reasoning --repair-rounds 6
python build_with_sonder.py --only Program.cs     # regenerate one file
python build_with_sonder.py --repair-only         # skip generation, just repair
```

The per-file contracts live in `specs.py`. They are deliberately
**transformation** prompts — the exact Raylib signatures, the exact field
names, the exact algorithm — never **recall** prompts. That distinction is
measured, not stylistic: on this setup the local model scores well when every
fact it needs is in the prompt, and badly when it has to remember an API. A
lookup table looks mechanical and is the worst case, because it is pure recall.

Target architecture (8 files): class kits, arena map with collision, combatants
and bot AI, a UDP wire protocol, match state with scoring and respawns, a
host/join lobby layer, the menu/lobby/HUD/scoreboard screens, and a Program that
drives the screen state machine.

### Result (2026-08-06)

**It did not reach a compiling build.** Final state: **109 distinct compiler
errors**, verified unmasked (see "the number I kept getting wrong" below).
Recorded here because a negative result that is measured is worth more than a
positive one that is asserted.

#### The number I kept getting wrong

During the run I reported "106 → 14 → 2 errors" as progress. That was false
three separate times, and the reason is worth more than the game:

**A build under-reports when the compiler stops early.** It runs parse →
declare → bind bodies. An error in an early phase means the printed total says
how far it *got*, not how broken the project *is*. Three distinct mechanisms hid
errors here:

| Mechanism | What it looked like | Reality |
|---|---|---|
| Parse errors | "2 errors" | 99 — the binder never ran |
| Namespace mismatch | 6 "type not found" | one file of eight wrapped itself in a namespace the others never declared |
| Malformed type (C# `CS0111`) | "1 error" | 109 — the type parsed but could not be declared, so no dependent bound |

The third one fooled me *after* I had built a parse-error guard and believed the
problem solved. A duplicate member is not a syntax error, so the guard passed it
as trustworthy.

`codegen_loop.count_unreliable()` now detects the first and third and marks such
a total as a FLOOR. The final 109 above was checked with it and is genuinely
unmasked. The second is deliberately not detected — an invisible type is
reported with the same "could not be found" line as a type that was never
written, which is the loop's most common *honest* error, and it inflates counts
rather than hiding them.

The general rule, which cost four wrong readings to learn: **an error count is
only comparable to another when nothing in either stopped the compiler short of
binding.**

#### The detector itself was the next bug

Having learned that, I built a masked-count detector — and matched on the
`CS1xxx`/`CS8xxx` error-code *range*. Ranges leak in both directions:

- `CS8180`, `CS8124` are the parser but sit outside `CS1xxx` — **missed**.
- `CS1061`, `CS1503`, `CS1501` are **binder** errors that merely live inside it
  — **falsely flagged**. Measured on a build with zero real parse errors: 101
  errors, of which the detector claimed 36 were parse errors.

The false positives are the damaging half. They pin the "masked" bit at 1 for
every candidate, which collapses `score = (masked?, total)` back into ranking by
total — precisely the bug the tuple existed to prevent — and the loop then
discarded a generated file that had *genuinely fixed* the parse error, in favour
of one whose masked "1" was really 101.

Detection now matches message shape only, which also generalises across
toolchains as a code range never can.

**Baseline once honest: 99 errors, zero parse-shaped — a fully bound build.**
That was the first count in this whole exercise that was a total rather than a
floor, and therefore the first one worth optimising against.

#### The run that finally measured something

With a trustworthy baseline the loop ran properly: **99 → 85 errors, still zero
parse-shaped**, verified independently against `dotnet build`.

Per file: Screens 32, Program 22, MatchState 16, NetProtocol 5, LobbyNet 5,
Combatant 4, GameMap 1.

The interesting part is *where* the 14 came from. Across 7 files and 12
regeneration attempts:

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
climbs exactly that gradient, which is why the masked tier has to outrank on
blockers rather than count. This run rejected seven such candidates; the earlier
broken scorer accepted one and threw away the file that had genuinely fixed the
parse error.

The deterministic fixers did steady, unglamorous work throughout: wrong-library
calls rewritten, `System.Numerics`/`System.Net` imports added, and the
`namespace ArenaShooter` wrapper stripped from three separate files that each
re-added it.

Generation: 8 files, ~28 KB of C#, ~22 minutes of model time
(`code=sonder:latest` + `reasoning=deepseek-r1:7b`).

**What it got right.** Every algorithm it was handed a contract for.
`MoveWithSlide`'s axis-at-a-time resolution, the ray/sphere intersection
formula, all 24 class-stat values transcribed exactly. Where every fact needed
was in the prompt, the output was correct.

**What it got wrong, by failure class:**

| Class | Evidence |
|---|---|
| Dropped declarations | `MapData` declared `static readonly` and never assigned — *not a compile error*, would have null-referenced at runtime. `public enum Screen` was in the spec verbatim and simply absent. |
| Recalled the wrong API | `Vector3.position`, `Vector3.normalized` — **Unity** idioms, not `System.Numerics`, despite the exact API being in the prompt. |
| Invented members | `GameMap.GetDistance`, `GameMap.WorldSize`, `GameMap.GetMoveSpeed` — none specified, none existing. |
| Cross-file contract drift | `ClassId.Get` for `ClassKit.Get` (15 × CS0117); fields specced `public` emitted as `{ get; }`, so every assignment failed (16 × CS0272). |
| Character-level blindness | `wish -= me FlatRight` (missing `.`). Shown the line and the exact compiler error, it echoed the broken line back **verbatim, twice**. |

Final error mix: CS1061 member-not-found (28), CS0103 name-not-found (21),
CS0272 assign-to-readonly (16), CS0117 no-such-definition (15), CS1503
argument-mismatch (13).

**The harness lesson is the transferable one.** A naive compile-and-repair loop
*converges on deletion*: removing code is always a valid way to silence a
compiler. Measured, asked only to fix syntax:

- `Program.cs` repair returned **44%** of the original (two missing `.`)
- `Screens.cs` repair returned **13%** of the original (one missing type name)

Without the `SHRINK_FLOOR` guard this harness would have reported
BUILD SUCCEEDED on a gutted game. Any agent running an automated fix loop needs
that guard, or its green builds mean nothing.

Two mechanical steps were worth far more than model repair: adding the missing
`using` directives deterministically took the count **50 → 2** in one pass, and
per-file repair provably cannot fix a defect whose *cause* is in another file
(the `Screen` enum error surfaced in `Program.cs`).

Human intervention applied, for full disclosure: 3 characters (two `.`, one
`int`), one dropped `enum` declaration restored from the spec, one duplicate
`CellCenter` removed (both definitions were textually equivalent), and the
deterministic `using`/namespace/generic-syntax rewrites, which are mechanical
and in the harness rather than hand-edits. Everything else is as generated. The
remaining 109 errors were left in place — patching them would have made this my
code rather than a measurement.

Did feeding each file the **real extracted API** of its dependencies fix the
cross-file drift? **No.** `Combatant.cs` was handed `GameMap`'s actual
signatures and still called `GetDistance`, `WorldSize`, `ClassId.Get` and
Unity's `Vector3.normalized` — four members that appear in neither the contract
nor the extracted API. Being shown the truth did not stop it inventing.

**The honest read:** a 7B-class local model writes correct *functions* and
cannot hold a *system*. It has no working memory of the contract between files,
even with that contract in every prompt.
