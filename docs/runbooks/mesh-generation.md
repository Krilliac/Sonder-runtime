# Mesh generation (`mesh_generate`)

`mesh_generate` turns a text brief into a low-poly 3D model (GLB or FBX) by letting a model
write build scripts and checking them against renders. It is a closed loop over
several candidates:

1. A model writes a build script in a small Python dialect (`def build():`; kit and core calls,
   plain Python control flow; see "Safety model" for what is allowed).
2. The host **records** the script: an interpreter in this server follows the script and writes
   the kit and core calls it makes into a JSON op list (`<stem>.ops.json`). The script itself never
   executes. A script the recorder cannot accept goes back to the model as a `script_error` with
   the line number and a repair hint; that costs a round.
3. Headless Blender replays the op list with trusted code (`harness.py`, `replay.py`, the kit),
   joins the parts into one mesh, scales it (when
   `height_m` is set), stands it on the ground, counts triangles, exports it, and renders a
   2x2 contact sheet (front from -Y, right from +X, three-quarter, top) plus the three-quarter
   view alone.
4. A build error, or a triangle count over `max_tris`, goes back to the model as a repair prompt.
5. A vision model scores the sheet (0-10) and lists concrete issues. Below `target_score` the
   score and issues go back to the model as a revision prompt; at or above it the candidate stops.
6. Candidates are ranked: built, then within budget, then vision score, then fewer triangles.
   The best one wins and is copied to `<output_dir>/<name>.glb` (or `.fbx`), `<name>_sheet.png`,
   `<name>_view.png`, `<name>.py` and `<name>.ops.json`.

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

- `kit_path` names a Python helper module that candidate scripts may `import` and call (it is
  analysed with `ast` on the host for the manifest, and its directory is put on Blender's `sys.path`
  for the replay; a script only ever *names* its functions). Without `api_doc_path` the prompt carries a digest of the
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
12 below 2000, 16 below 6000, 24 below 20000, 32 above) and smooth shading on organic parts. Any
modifier type is allowed (including `NODES`); the harness bakes them and counts triangles after
modifiers, and literal counts such as subdivision `levels` are capped. The default is
3000. For hero assets pass 8000-20000 or more (the clamp is 50..200000); generation allows 8192 reply
tokens. An over-budget result is repaired with the biggest parts named. The critic also flags blocky,
low-effort results when the budget allowed more.

If a candidate repeats the same build error, or the same top critique issue with the same score and
about the same triangle count, the next prompt says so explicitly (placed before the closing
"Return ONLY ..." line) and, with more than one tier, the remaining rounds of that candidate move to
a different tier, preferring one that candidate has not used yet. The winner is credited with the
tier that produced its version, and each candidate reports `tiers_used`.

## Safety model

**Model-written code never executes.** An earlier design ran the model's Python in Blender behind an
AST gate and a secret-pattern guard. It could not be made safe: CPython's object graph is fully
connected (a generator's `gi_frame.f_back.f_globals` reaches the harness' real `importlib`), Blender
ships `winreg`, `_winapi`, `nt` and `msvcrt` as builtin modules, and no pattern guard can bound
encodings or numeric channels. So the model's script is now **data**, not code:

| Layer | Job |
| --- | --- |
| **Recorder** (`domain/meshgen/dsl/`, pure stdlib, host side) | An allow-list interpreter. It evaluates the dialect itself and only *records* calls to the kit and to the core functions into an op list. Values are a closed domain: `None`, bool, int below 2^63, finite float, capped str, list/tuple/dict of those, an opaque result handle and a face predicate. A function, module, type, frame, generator, exception or `bpy` object cannot exist in the interpreter. No generic dispatch: attributes exist only on the `math`/`random`/kit aliases and on a fixed method table for str/list/tuple/dict; the recorder source itself never calls `exec`, `eval`, `compile`, `__import__`, `getattr`, `setattr`, `type`, `vars`, `globals` or `locals` and imports only `ast`, `math`, `random`, `json`, `time` and its own siblings (a static test enforces this). |
| **Replay and harness** (`adapters/meshgen/harness.py`, `replay.py`, `dsl/opschema.py`, trusted, inside Blender) | The harness validates `ops.json` against the same grammar the recorder emits (fail closed: an unknown op, a forward or undefined `$ref`, an unknown key, an oversized value or file refuses the whole build before anything is created), then `replay.py` performs each op with trusted code: the core functions (`primitive`, `mesh`, `transform`, `shade_smooth`, `modifier`, `paint`, `paint_faces`, `join`, `delete`, with size caps and a per-modifier property allow-list) and the exposed kit functions. Face predicates are evaluated by a small recursive evaluator, never `eval`. |
| **OS sandbox** (`sandbox_win.py`: zero-capability AppContainer, Job Object, private desktop) | **Defence in depth** for the trusted code: it can read and write only its run directory (and read the Blender install), has no network and cannot start a process. |

What the recorder budgets: 2,000,000 interpreter steps, 10 s wall clock (checked every 10,000 steps),
call depth 64, containers of at most 200,000 elements, ints below 2^63, at most 5,000 recorded calls, a
4 MB `ops.json` and a 60,000 character script. Anything over a cap is a `script_error` naming the line.

**`ops.json` grammar.** `{"version":1, "kit", "seed", "ops":[{"id","line","op","args","kwargs"}], "log"}`.
`op` is `core.<name>` or `kit.<name>` with a name the manifest exposes. A value is `null`, bool, a finite
number, a string of at most 256 characters, an array, or an object with exactly one key: `$ref` (the id of
an *earlier* op) or `$pred` (a predicate tree). Nesting is at most 8 and a file holds at most 2,000,000
leaves. Rotations are in degrees everywhere.

**Handles.** Every kit/core call returns a fresh opaque handle. It can be passed to other kit/core
functions (including inside lists) but not inspected: no indexing, arithmetic, attributes, unpacking or
comparison. There are no measurements while the script is recorded; the model computes positions from the
sizes it passes. The repair prompt says so when a script tries.

**Predicates.** `paint_faces(obj, lambda p: p.center.z > 0.5, "#rrggbb")` (and any kit parameter the kit's
body calls, for example `paint_faces.predicate`, or the operator's `MESHGEN_PREDICATES` literal) is the one
place a `lambda` is allowed. The lambda is serialised into a nested-array tree (face attributes in
object-local coordinates, arithmetic, comparisons, `and`/`or`/`not`, conditional expressions,
`abs`/`min`/`max`/`round` and `math.*`; at most 32 deep and 2,000 nodes); free variables are captured by
value and must be numbers, bools or strings.

**Kit manifest.** Built on the host from the kit source with `ast` (never imported): the exposed names
(`kit_policy`), their signatures (so a wrong keyword produces an error listing the real parameters), literal
constants such as `PALETTE`, and the predicate parameters.

**Known gap: the registry stays readable.** Every AppContainer is in ALL APPLICATION PACKAGES, which the
user's HKCU keys (for example `HKCU\Environment` and `RecentDocs`) grant read. Closing it was tried and
abandoned: LPAC (opting out of that group) makes Blender exit with 0xC0000022 because side-by-side
manifest activation needs `HKLM\...\SideBySide`, which grants only that group and the `registryRead`
capability, and with that capability added rendering crashes (no GPU/OpenGL access) while HKCU is still
readable through the same capability; a DENY ACE for the package SID has no effect. `preflight` measures
it (`registry: readable` plus the warning "HKCU is readable by the Blender process; no model-written code
runs in it") and the strict-xfail test `test_hkcu_environment_and_recentdocs_are_unreadable_from_the_sandbox`
records it. The gap no longer matters for model code, which never runs there: only the trusted replay,
the harness and the operator's kit do, and they act on validated data.

**Residual risks.** The replay is trusted code that does what the (validated) ops say, so a hostile *number
or name* can still reach Blender as data: the grammar and the per-op caps bound it, and a kit function that
misbehaves on odd arguments is the operator's code. A kit that reads secrets and returns them in an object
name is not detected (there is no secret guard any more; the host only length-caps and pattern-filters the
text it passes on). The recorded script and the ops are kept next to the outputs. Model quality under the
restricted dialect is a measurement, not a guarantee.

Inside the sandbox the process can read and write only its own per-build run directory and read the
Blender install directory. User files, repositories, `.ssh` and credential files on disk are unreadable; there is
no network (loopback included); no second process can be started (`ActiveProcessLimit=1`); the job
has a memory limit (default 4096 MB) and is killed when its handle closes. The process is created
suspended and resumed only after `AssignProcessToJobObject` succeeded; if that or any other setup
step fails the build is refused. **Fail closed**: there is no silent fallback to running unsandboxed.

Verified with real Blender 5.2 in `tests/test_meshgen_integration.py`: a recorded Lanternfall
`lf_kit.box` builds, exports and renders through the sandboxed runner; core primitives work without a kit;
modifiers are baked; a `paint_faces` predicate yields two distinct vertex colours in the GLB; a kit
`LFError` comes back as `line N: ... LFError`; and the harness refuses hand-crafted `ops.json` files naming
`winreg.OpenKey`, `kit.finalize_and_export`, `core.__import__` or an undefined `$ref` (`ok: false`, no
files). `import winreg` in a script is a `script_error` before Blender starts.

Further isolation, each verified with real Blender and an unsandboxed control
(`tests/test_meshgen_sandbox_live.py`): writes outside the run dir, process spawn, symlink, junction
and hardlink escapes, loopback and external TCP, DNS, and screen capture (the build runs on a private
desktop) all fail in the sandbox. Two concurrent builds use different AppContainer profiles (three
slots, a cross-process mutex per slot) and cannot open each other's processes; the AppContainer's
own storage is wiped before every build so nothing persists between builds.

Probed and not fixable from inside this design (see also the registry gap above): `GetAsyncKeyState` returned 0 in both modes with no
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
file, the trusted harness files (`harness.py`, `replay.py`, `opschema.py`) and `ops.json` are *copied* into the run directory (only the single kit file is
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
| `SONDER_MESHGEN_UNSANDBOXED=1` | explicit opt-out: run Blender with the server's authority. The report then carries `sandbox: "unsandboxed"` and a `warning`; use only where Blender running with the server's authority is acceptable |
| `SONDER_BLENDER` | path to `blender.exe` |

If the sandbox cannot be set up (for example the Blender grant needs an account that may edit its
ACL, run the printed `icacls ... /grant` once) `mesh_generate` returns an error naming the cause and
the opt-out; it never starts a build first. Non-Windows hosts have no sandbox here: they fail closed
and run only with `SONDER_MESHGEN_UNSANDBOXED=1` (then reported as `unsandboxed`), and the POSIX memory ceiling is a best-effort `prlimit` on Linux only (no
`preexec_fn`, which is unsafe with threads).

Cost: roughly 2 s extra per build (profile and ACL work, a second process launch, copying the
harness files, ops and kit into the run directory).

### Other layers

- **Environment scrub**: Blender is started with an explicit allow-list environment, never a copy
  of the server's. Only `SYSTEMROOT`, `WINDIR`, `SYSTEMDRIVE`, `PATHEXT`, `PROCESSOR_ARCHITECTURE`,
  `NUMBER_OF_PROCESSORS`, `OS`, `LANG`, `LC_ALL` are copied; `PATH` is just the Blender directory
  plus the system directories; `TEMP`/`TMP`/`HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` point at a
  per-build scratch directory (`<candidate dir>/.blender_profile`, deleted after every build) on the unsandboxed path (the sandbox uses the run directory). API keys, tokens and `SONDER_*` values never reach the process.
- **Kit exposure** (`sonder_runtime/domain/meshgen/kit_policy.py`). A kit is trusted code, and its
  exposed public functions are the only kit entry points a recorded script can name. Plain file reads and writes no longer
  exclude a function (the sandbox confines them). Exposure is an allow-list derived from the kit
  source: a function or class is excluded when anything in it touches scene-level export/import/save
  or render (`export_scene`, `import_scene`, `save*`, `ops.render`, `open_mainfile`, `read_homefile`...:
  the harness exports and renders itself, and a kit helper doing it too would clobber the harness'
  output), process control (`exit`, `exec`/`eval`/`compile`/`__import__`), or one of the modules that read
  secrets or spawn processes (`os` except path-string helpers like `os.path`, `sys`, `subprocess`, `socket`,
  `ctypes`, `winreg`, `importlib`, `builtins`, `pickle`, `marshal`, `urllib`, `http`; `system`/`popen`
  only on such receivers); location constants (`KIT_DIR`, `REPO_ROOT`, ...) and text constants are
  never exposed, and exclusion is transitive (a function that uses an excluded location is excluded).
  For the Lanternfall `lf_kit.py` this excludes `COL`, `KIT_DIR`, `PREVIEW_DIR`, `PROPS_OUT_ROOT`,
  `REPO_ROOT`, `finalize_and_export`, `render_preview`, `result_line` and `run_prop` (`reset_scene` stays
  available). The operator can override the derivation with module-level literals in the kit:
  `MESHGEN_ALLOW = ["box", "lathe"]` replaces the derived list (only public names the kit defines
  count) and `MESHGEN_EXCLUDE = ["helper"]` removes names from it. Excluded names never appear in the
  API digest or repair hints; the prompt tells the model that the harness exports and renders. The
  harness also refuses any op naming a kit function that is not in the job's `kit_allowed` list. A
  user-supplied `api_doc_path` text is passed through unchanged and is the operator's responsibility.
- **Blender flags and plumbing**: `--factory-startup --disable-autoexec`, no shell, `cwd` the
  output directory, a per-build timeout. The harness reports its result through a
  per-job, unguessable file (not stdout, which a kit function can write to), and every file the
  result names must resolve inside the candidate's output directory.

- **Memory ceiling for the unsandboxed path** (best effort, `blender_runner.run_limited`): a Job
  Object with a per-process committed-memory cap (default 8192 MB; brief window between start and
  assignment), or on Linux `prlimit(RLIMIT_AS)` right after start (three times the cap). The Linux
  path is written but was not exercised on this Windows host.

Without the sandbox (opt-out or a non-Windows host) the OS layer is gone, but model code still never
runs: the recorder and the harness grammar are what keep a script from reaching anything. A Blender bug
or a misbehaving kit function then has this server's authority and nothing else limits what Blender itself
can reach. Run the tool unsandboxed only where that is acceptable.

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
- Every attempt `script_error`: read the messages in `attempts[].issues`; they name the line and the rule
  (a result handle used as a number, an unsupported statement, a wrong kit argument).
