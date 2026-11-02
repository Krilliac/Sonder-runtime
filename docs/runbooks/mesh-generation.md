# Mesh generation (`mesh_generate`)

`mesh_generate` turns a text brief into a low-poly 3D model (GLB or FBX) by letting a model
write Blender build scripts and checking them against renders. It is a closed loop over
several candidates:

1. A model writes a Blender Python script (`def build():`).
2. A safety gate (AST check) screens the script. A rejected script goes back to the model with
   the reasons; that costs a round.
3. Headless Blender builds the script, joins the parts into one mesh, scales it (when
   `height_m` is set), stands it on the ground, counts triangles, exports it, and renders a
   2x2 contact sheet (front from -Y, right from +X, three-quarter, top) plus the three-quarter
   view alone.
4. A build error, or a triangle count over `max_tris`, goes back to the model as a repair prompt.
5. A vision model scores the sheet (0-10) and lists concrete issues. Below `target_score` the
   score and issues go back to the model as a revision prompt; at or above it the candidate stops.
6. Candidates are ranked: built, then within budget, then vision score, then fewer triangles.
   The best one wins and is copied to `<output_dir>/<name>.glb` (or `.fbx`), `<name>_sheet.png`,
   `<name>_view.png` and `<name>.py`.

Candidates rotate through `tiers` (default `general,code`), so a run mixes models.

## Requirements

- **Blender 4 or 5** on the machine that runs the server. Set `SONDER_BLENDER` to the full path
  of `blender.exe` (or put `blender` on `PATH`). Without it the tool returns an error payload
  naming `SONDER_BLENDER`; nothing else runs.
- **A vision model** for the critique step. It is optional: without one the loop still builds
  and repairs, but cannot revise for looks, and the report says `"vision": "unavailable: ..."`.
  Enable it with `/runtime set vision=qwen3-vl:8b` (the vision tier needs a native context
  large enough for one image; see `/runtime status`).

## Using it

```text
mesh_generate(brief="a low-poly wooden handcart with two wheels", max_tris=1500, height_m=1.2)
```

Useful arguments: `candidates` (1-6, default 3), `rounds` (1-6, default 3), `tiers`, `target_score`,
`export` (`glb` or `fbx`), `kit_path` and `api_doc_path`, `output_dir`, `timeout`, `vision`.

- `kit_path` names a Python helper module that candidate scripts may `import` (its directory
  is put on Blender's `sys.path`). Without `api_doc_path` the prompt carries a digest of the
  kit's public functions and first docstring lines.
- Output goes under the asset workspace (`artifacts/generated/meshgen/<name>-<UTC stamp>` by
  default; `output_dir` must stay inside `SONDER_ASSET_WORKSPACE`/the checkout). Each round of each
  candidate keeps its script, model and renders in `c<candidate>_r<round>/`.
- The result is compact JSON: the winner (paths, triangle count, size, score), per-candidate
  attempts (status, triangles, score, issues, seconds) and the `vision` state. Code bodies are
  never returned; the script paths are.

## Budgets and quality

`max_tris` is a target to use, not just a ceiling: the prompt asks for roughly 60-90% of it, with
bevels on hard edges, enough segments that round parts read round (about 10 below 500 triangles,
12 below 2000, 16 below 6000, 24 below 20000, 32 above) and smooth shading on organic parts. Modifiers
(`BEVEL`, `SUBSURF`, `SOLIDIFY`, `MIRROR`, `ARRAY`, `WEIGHTED_NORMAL`, `TRIANGULATE`, `DECIMATE`,
`BOOLEAN`) are allowed; the harness bakes them and counts triangles after modifiers. The default is
3000. For hero assets pass 8000-20000 or more (the clamp is 50..200000); generation allows 8192 reply
tokens. An over-budget result is repaired with the biggest parts named. The critic also flags blocky,
low-effort results when the budget allowed more.

If a candidate repeats the same build error, or the same top critique issue with the same score and
about the same triangle count, the next prompt says so explicitly and, with more than one tier, the
remaining rounds of that candidate move to the next tier.

## Safety model

`mesh_generate` runs **model-written Python**. It is graded as an execution tool, the same
trust level as `run_code`, and asks or is denied in the same permission modes.

Several layers reduce accidents, but they are defence in depth, **not a sandbox**:

- **AST gate** (`sonder_runtime/domain/meshgen/script_safety.py`). It is an allow-list for
  Blender, not a deny list of paths:
  - `bpy` may be imported only as plain `import bpy`, and is usable only as
    `bpy.ops.<category>.<operator>(...)` with category `mesh`, `object`, `transform` or `curve`;
    `bpy.data.<collection>` with `objects`, `meshes`, `materials`, `collections`, `curves`; and
    `bpy.context.<member>` (scene, view_layer, active/selected objects, depsgraph). `bpy`,
    `bpy.ops`, `bpy.data` and `bpy.context` cannot be assigned, passed, stored, defaulted or
    returned, so there is no alias to launder a path through. Operator names containing
    import/export/save/load/open/write/script/python/bake/browse/file are refused.
  - A fixed list of attribute names is refused on any object, including as keyword arguments:
    `as_module`, `blend_data`, `texts`, `text`, `libraries`, `filepath`, `filepath_raw`, `save*`,
    `write`, `driver*`, `expression`, `utils`, `app`, `handlers`, `preferences`, `window_manager`,
    `types`, `rna_type`, `id_data`, `os`, `sys`, `subprocess`, `shutil`, `importlib`, `io`,
    `socket`, `ctypes` and more (see `FORBIDDEN_ATTRIBUTES`). `write` is refused even on
    non-bpy objects. A few string literals naming data blocks (`"texts"`, `"blend_data"`, ...)
    are refused too.
  - Imports are limited to `bpy`, `bmesh`, `mathutils`, `math`, `random` and the kit module;
    `open`, `exec`, `eval`, `getattr`, `type`, `dir` and friends and every name or attribute
    starting with `_` (except a bare `_`) are refused.
  - The kit module is reachable only through the public top-level functions, classes and
    constants of the kit source (imports and re-exports such as `kit.Vector` are not
    available). A kit's own private helpers are therefore unusable by model scripts.
  - `str.format` and `format_map` (they resolve attribute chains from string text, e.g. a format
    field reaching a function's globals) and any string literal containing `__` are refused;
    `%` and f-string formatting stay available.
- **Restricted builtins** inside Blender (`harness.py`): a small safe subset, and `__import__`
  limited to the same module allow-list.
- **Environment scrub**: Blender is started with an explicit allow-list environment, never a copy
  of the server's. Only `SYSTEMROOT`, `WINDIR`, `SYSTEMDRIVE`, `PATHEXT`, `PROCESSOR_ARCHITECTURE`,
  `NUMBER_OF_PROCESSORS`, `OS`, `LANG`, `LC_ALL` are copied; `PATH` is just the Blender directory
  plus the system directories; `TEMP`/`TMP`/`HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` point at a
  per-build scratch directory. API keys, tokens and `SONDER_*` values never reach the process.
- **Minimal helper globals**: the injected `paint` helper is built in a fresh namespace holding five
  harmless builtins, so reflection on it reaches nothing of the harness.
- **Harness hardening**: before the script runs, `Text.as_module` and `bpy.utils.execfile`
  are replaced with functions that refuse, so a script that somehow reached a text block
  still cannot execute it with unrestricted builtins.
- **Blender flags and plumbing**: `--factory-startup --disable-autoexec`, no shell, `cwd` the
  output directory, a per-build timeout. The harness reports its result through a
  per-job, unguessable file (not stdout, which the script can write to), and every file the
  result names must resolve inside the candidate's output directory.

What is NOT guaranteed: an allowed operator or a Blender bug can still do harm, a script can
burn CPU or memory until the timeout, and nothing here limits what Blender itself can reach.
Run the tool only where `run_code` would be acceptable.

Reads are guarded: `kit_path` and `api_doc_path` go through the same guarded reader as the
file tools (`file_ops.require_read_access`), so they must sit inside the allowed file roots
(`SONDER_FILE_ROOTS`) and credential stores such as `.ssh` are refused before anything can
reach a prompt.

## Troubleshooting

- `Blender not found`: set `SONDER_BLENDER`.
- `no result; rc=N` in an attempt: Blender crashed before reporting; the `tail` lines in the
  attempt show the last output. Try a lower `max_tris` or fewer parts.
- `vision: unavailable`: the vision tier is unset, not loopback, or the model is not pulled.
  The first vision error disables critique for the rest of the run.
- Every attempt `rejected`: read the reasons in `attempts[].issues`; they name the line and the rule.
