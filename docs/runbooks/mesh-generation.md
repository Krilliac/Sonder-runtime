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
  kit's exposed functions and first docstring lines. Which kit names are exposed is a policy, see
  "Kit exposure" below.
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
`BOOLEAN`, `EDGE_SPLIT`, `SMOOTH`, `WELD`, `SCREW`) are allowed as literal type strings only (any other
type, or a computed one, is refused: `NODES`, `DISPLACE`, `WAVE`, `HOOK` ... can reach other data); the harness bakes them and counts triangles after modifiers. The default is
3000. For hero assets pass 8000-20000 or more (the clamp is 50..200000); generation allows 8192 reply
tokens. An over-budget result is repaired with the biggest parts named. The critic also flags blocky,
low-effort results when the budget allowed more.

If a candidate repeats the same build error, or the same top critique issue with the same score and
about the same triangle count, the next prompt says so explicitly (placed before the closing
"Return ONLY ..." line) and, with more than one tier, the remaining rounds of that candidate move to
a different tier, preferring one that candidate has not used yet. The winner is credited with the
tier that produced its version, and each candidate reports `tiers_used`.

## Safety model

`mesh_generate` runs **model-written Python**. It is graded as an execution tool, the same
trust level as `run_code`, and asks or is denied in the same permission modes.

### Threat model: the OS sandbox is the boundary

On Windows every Blender build runs inside a **zero-capability AppContainer** with a Job Object
(`sonder_runtime/adapters/meshgen/sandbox_win.py`). That sandbox is the security boundary. The AST
gate, the kit policy and the harness' restricted builtins below are *first filters*: they catch
mistakes cheaply, give the model repair feedback and keep the prompt honest, but a static check
can always be bypassed and none of it is relied on to contain a hostile script.

Inside the sandbox the process can read and write only its own per-build run directory and read the
Blender install directory. User files, repositories and credential stores on disk are unreadable; there is
no network (loopback included); no second process can be started (`ActiveProcessLimit=1`); the job
has a memory limit (default 4096 MB) and is killed when its handle closes. The process is created
suspended and resumed only after `AssignProcessToJobObject` succeeded; if that or any other setup
step fails the build is refused. **Fail closed**: there is no silent fallback to running unsandboxed.

Verified with real Blender 5.2 in `tests/test_meshgen_integration.py`, each with an unsandboxed
control showing the probe really would succeed: reading a canary file outside the run dir, a TCP
connection to a loopback listener, and `wm.obj_import` of a canary file all fail in the sandbox; the
Lanternfall `lf_kit.box` builds, exports and renders through it.

Further isolation, each verified with real Blender and an unsandboxed control
(`tests/test_meshgen_sandbox_live.py`): writes outside the run dir, process spawn, symlink, junction
and hardlink escapes, loopback and external TCP, DNS, and screen capture (the build runs on a private
desktop) all fail in the sandbox. Two concurrent builds use different AppContainer profiles (three
slots, a cross-process mutex per slot) and cannot open each other's processes; the AppContainer's
own storage is wiped before every build so nothing persists between builds.

**KNOWN GAP (review B1): the sandbox can READ the user's `HKCU` registry** (for example
`HKCU\Environment`, which may hold API keys the user set as user environment variables, and
`RecentDocs`). Every AppContainer is a member of ALL APPLICATION PACKAGES, which the user's HKCU keys
grant read. Measured: a Blender script can read these values and could leak them through an exception
message; the host redacts secret-looking text and caps every returned string at about 2 KB, which is a
backstop, not a boundary. What was tried: LPAC (opting out of ALL APPLICATION PACKAGES) makes Blender
exit with 0xC0000022 because side-by-side manifest activation needs `HKLM\...\SideBySide`, which
grants only that group and the `registryRead` capability; adding that capability restores startup but
the same capability is also in the user's HKCU key ACLs (so HKCU stays readable) and rendering then
crashes (no GPU/OpenGL driver access). A DENY ACE for the package SID on a key had no effect. The
test `test_hkcu_environment_and_recentdocs_are_unreadable_from_the_sandbox` is a strict xfail that
will start failing the day this is fixed. Options for the operator: keep secrets out of user-level
environment variables and the HKCU hive while using this tool; run the server under a separate
low-privilege Windows account (or a VM) with an empty HKCU; or deny ALL APPLICATION PACKAGES on the
HKCU keys yourself (invasive, breaks UWP apps). `preflight` reports `registry: readable` with a warning.

Probed and not fixable from inside this design: `GetAsyncKeyState` returned 0 in both modes with no
key pressed (inconclusive; the private desktop is the mitigation); the POSIX `prlimit` path was not
run on this machine.

What the sandbox does not claim: it does not stop CPU use until the timeout, nor a Windows
AppContainer escape; the opt-out and non-Windows platforms have no sandbox at all.

**ACL footprint** (everything the sandbox changes on the machine, and how to undo it):

- three AppContainer profiles `Sonder.MeshGen.s0`..`s2` for the current user (created on first use);
- a private desktop per build in the current window station (closed afterwards);
- one persistent grant per profile, added once and only if missing: `(OI)(CI)(RX)` for that profile's SID on the
  Blender install directory (validated first: symlinks and shims are resolved, and the home directory,
  its ancestors and descendants, drive roots and any directory containing the kit are refused). Undo: `icacls "<blender dir>" /remove:g *<SID>` (the SID is printed by
  `icacls "<blender dir>"`; the profile itself can be removed with `DeleteAppContainerProfile`);
- per build, a fresh run directory `<candidate dir>/.run-<id>` with a `(OI)(CI)(M)` grant that is
  removed, and the directory deleted, when the build ends (also on failure/timeout). Builds run in
  parallel, each with its own run directory, grant, job and handles.

No grant is ever placed on the kit's source directory, the repository or any user directory; a run
directory that would overlap the kit's directory (or contain the user's home) is refused. The kit
file, the harness and the script are *copied* into the run directory (only the single kit file is
copied, so a kit that imports sibling modules will not work in the sandbox). Outputs are copied back
to the candidate directory afterwards.

The sandboxed process gets a minimal environment: `SYSTEMROOT`/`WINDIR`, a `PATH` of the Blender and
system directories, `TEMP`/`TMP`/`HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` and
`BLENDER_USER_{CONFIG,SCRIPTS,DATAFILES,EXTENSIONS}` all inside the run directory, and nothing from the
server's environment. (Blender 5.2 has no `--temp-dir` option; it honours `TEMP`, which points into
the run directory.)

Environment variables:

| Variable | Meaning |
| --- | --- |
| `SONDER_MESHGEN_MEM_MB` | job-wide memory limit in MB (default 4096, clamped 256..65536); a build that hits it is reported as `memory limit exceeded (N MB)` |
| `SONDER_MESHGEN_UNSANDBOXED=1` | explicit opt-out: run Blender with the server's authority. The report then carries `sandbox: "unsandboxed"` and a `warning`; use only where `run_code` would be acceptable |
| `SONDER_BLENDER` | path to `blender.exe` |

If the sandbox cannot be set up (for example the Blender grant needs an account that may edit its
ACL, run the printed `icacls ... /grant` once) `mesh_generate` returns an error naming the cause and
the opt-out; it never starts a build first. Non-Windows hosts have no sandbox here: they fail closed
and run only with `SONDER_MESHGEN_UNSANDBOXED=1` (then reported as `unsandboxed`), and the POSIX memory ceiling is a best-effort `prlimit` on Linux only (no
`preexec_fn`, which is unsafe with threads).

Cost: roughly 2 s extra per build (profile and ACL work, a second process launch, copying the
harness, script and kit into the run directory).

### Defence-in-depth layers (not the boundary)

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
  - The kit module is reachable only through the exposed names described under "Kit exposure"
    (imports and re-exports such as `kit.Vector` are not available, and private helpers are
    unusable by model scripts).
  - `**` and `*` argument unpacking is refused on `bpy` calls, `path_resolve` is refused, operator
    names containing node_group/asset/library are refused, and literal numeric arguments or
    assignments named `segments`, `ring_count`, `vertices`, `levels`, `count`, `cuts`, `steps`,
    `resolution`, `iterations` ... are capped (see `ARGUMENT_CAPS`) so one literal cannot request
    a mesh that exhausts memory.
  - `str.format` and `format_map` (they resolve attribute chains from string text, e.g. a format
    field reaching a function's globals) and any string literal containing `__` are refused;
    `%` and f-string formatting stay available.
- **Restricted builtins** inside Blender (`harness.py`): a small safe subset, and `__import__`
  limited to the same module allow-list.
- **Environment scrub**: Blender is started with an explicit allow-list environment, never a copy
  of the server's. Only `SYSTEMROOT`, `WINDIR`, `SYSTEMDRIVE`, `PATHEXT`, `PROCESSOR_ARCHITECTURE`,
  `NUMBER_OF_PROCESSORS`, `OS`, `LANG`, `LC_ALL` are copied; `PATH` is just the Blender directory
  plus the system directories; `TEMP`/`TMP`/`HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` point at a
  per-build scratch directory (`<candidate dir>/.blender_profile`, deleted after every build) on the unsandboxed path (the sandbox uses the run directory). API keys, tokens and `SONDER_*` values never reach the process.
- **Kit exposure** (`sonder_runtime/domain/meshgen/kit_policy.py`). A kit is trusted code, but its
  public functions are reachable from untrusted scripts and some of them write files (export,
  preview render, "run the whole prop"). Exposure is therefore an allow-list derived from the kit
  source: a function or class is excluded when anything in it touches `open`/`exec`/`eval`, the
  `os`/`sys`/`shutil`/`pathlib`/`subprocess`/`io`/`json`... modules, `Path`, file-ish attributes
  (`save`, `write`, `filepath`, `makedirs`, `export_scene`, `open_mainfile`, `ops.render`, ...; `system`/`popen` only on `os`/`subprocess`-like receivers); text
  constants (`KIT_DIR`, `REPO_ROOT`, ...) are never exposed; and exclusion is transitive (a function
  that uses an excluded kit name is excluded). For the Lanternfall `lf_kit.py` this excludes
  `COL`, `KIT_DIR`, `PREVIEW_DIR`, `PROPS_OUT_ROOT`, `REPO_ROOT`, `finalize_and_export`,
  `render_preview`, `result_line` and `run_prop` (`reset_scene` stays available). The operator can override the
  derivation with module-level literals in the kit: `MESHGEN_ALLOW = ["box", "lathe"]` replaces the
  derived list (only public names the kit defines count) and `MESHGEN_EXCLUDE = ["helper"]` removes
  names from it. Excluded names never appear in the API digest or repair hints; the prompt tells the
  model that the harness exports and renders. As defence in depth the harness replaces every
  excluded *function* in the loaded kit module with a stub that raises (constants are left alone,
  because exposed kit functions read them, e.g. `COL`), so a gate bug still cannot reach kit code that writes files. A user-supplied `api_doc_path`
  text is passed through unchanged and is the operator's responsibility.
- **Minimal helper globals**: the injected `paint` helper is built in a fresh namespace holding five
  harmless builtins, so reflection on it reaches nothing of the harness.
- **Harness hardening**: before the script runs, `Text.as_module` and `bpy.utils.execfile`
  are replaced with functions that refuse, so a script that somehow reached a text block
  still cannot execute it with unrestricted builtins.
- **Blender flags and plumbing**: `--factory-startup --disable-autoexec`, no shell, `cwd` the
  output directory, a per-build timeout. The harness reports its result through a
  per-job, unguessable file (not stdout, which the script can write to), and every file the
  result names must resolve inside the candidate's output directory.

- **Memory ceiling for the unsandboxed path** (best effort, `blender_runner.run_limited`): a Job
  Object with a per-process committed-memory cap (default 8192 MB; brief window between start and
  assignment), or on Linux `prlimit(RLIMIT_AS)` right after start (three times the cap). The Linux
  path is written but was not exercised on this Windows host.

Without the sandbox (opt-out or a non-Windows host) none of the above is a boundary: an allowed
operator or a Blender bug can still do harm, a script can burn CPU until the timeout, and nothing
else limits what Blender itself can reach. Run the tool unsandboxed only where `run_code` would be
acceptable.

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
