# Crash fixtures (lane A: pure crash readers)

Provenance of every file here. Paths inside recorded output were rewritten
to `/src/sparkmini` (Linux) or kept as the CI-style `C:\agent\_work\3\s`
paths the builds were told to record. No fixture contains process memory
beyond the tiny synthetic programs below.

## Recorded on the Linux dev host

| File | How it was produced |
|---|---|
| `crasher.cpp` | Source of the Linux crasher (`null`, `uaf`, `overflow`, `ubsan`, `leak`, `abort`, `hang`). |
| `gdb_segv.txt` | gdb 15.1 run with `domain.debugging.templates.gdb_template` materialized with nonce `0123456789abcdef` on a core from `gdb -batch -ex run -ex 'generate-core-file core.null' --args ./crasher null`. |
| `lldb_bt.txt` | lldb 18.1.3 with `lldb_template`, same core and nonce. |
| `eu_stack.txt` | eu-stack (elfutils 0.190) with `eu_stack_template`, same core. |
| `asan_uaf.log`, `asan_overflow.log`, `asan_segv.log`, `lsan_leak.log` | `g++ -g -O0 -fsanitize=address crasher.cpp`, modes `uaf`, `overflow`, `null`, `leak`. |
| `ubsan.log` | `g++ -g -O0 -fsanitize=undefined`, mode `ubsan`, `UBSAN_OPTIONS=print_stacktrace=1`. |
| `tsan.log` | `g++ -g -O0 -fsanitize=thread` on a two-thread counter race. |
| `memcheck.xml`, `memcheck_uaf.xml` | valgrind 3.22 `--xml=yes` on `crasher null` and `crasher uaf`. |
| `symbolizer_json.txt` | llvm-symbolizer-18 `--no-debuginfod --inlines --demangle --relative-address --output-style=JSON 0x1006 0x1003` on `pe/a/spark_tiny.exe` staged next to its PDB. |

## PE/PDB pairs (`pe/`)

Built by `pe/build.sh` with clang-cl-18 `/Z7` and lld-link-18 `/debug`
(`/pdbaltpath:C:\build\out\spark_tiny.pdb`, command line not recorded).
`a/` and `b/` are the same program name built twice with a different
constant, so their GUIDs differ: `pdb_matches(a.exe, b.pdb)` must be false.

| Pair | llvm-pdbutil GUID | Age | RSDS path |
|---|---|---|---|
| `pe/a` | `{EC756AB6-D819-44D7-4C4C-44205044422E}` | 1 | `C:\build\out\spark_tiny.pdb` |
| `pe/b` | `{07C7C727-1A90-3F7C-4C4C-44205044422E}` | 1 | `C:\build\out\spark_tiny.pdb` |

Age rule (spec F14 / risk 7): lld-link writes the same age to the PDB info
stream and the DBI stream, so these fixtures cannot tell the two apart. The
MSVC `/INCREMENTAL` check is Windows live-validation step 6.

## Hand-authored (flagged for replacement)

These follow the documented output shapes but were not produced by the tool
on this host (no Windows, no rust minidump-stackwalk). Windows live
validation step 7 replaces the cdb transcripts with real redacted ones.

- `cdb_av.txt`, `cdb_gs.txt`, `cdb_cpp_eh.txt`: cdb with the lane A template
  (nonce `0123456789abcdef`).
- `stackwalk_json.txt`: rust minidump-stackwalk 0.27 `--json` schema.
- `stackwalk_m.txt`: Breakpad `minidump_stackwalk -m` pipe format.
- `sample.ips`: macOS 14 `.ips` (header line + JSON body).

Synthetic minidumps are built in-test by `tests/support/minidump_builder.py`;
synthetic ELF cores (including PN_XNUM) by the helpers in
`tests/test_crash_elf_core.py`.
