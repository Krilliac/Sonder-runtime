"""Drive Sonder's local model ensemble to author, compile and repair a C# game.

Sonder writes every line of game code. This script is only the harness: it puts
one tightly-scoped contract per file to the ensemble, writes what comes back,
runs the real compiler, and feeds the actual compiler errors back for repair
rounds until the project builds or the round budget runs out.

Contracts live in specs.py and are deliberately *transformation* prompts (every
API and algorithm the model needs is in the prompt) rather than *recall* prompts,
because that is measurably where a 7B-class local model performs.

Usage:
    python build_with_sonder.py [--project PATH] [--tiers code,reasoning]
    python build_with_sonder.py --only Program.cs      # regenerate one file
    python build_with_sonder.py --repair-only          # skip generation, just repair
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

SONDER = os.environ.get("SONDER_RUNTIME", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PROJECT = os.path.abspath(
    os.path.expanduser(
        os.environ.get(
            "SONDER_GAME_PROJECT",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "FpsGame_Sonder"),
        )
    )
)

# A repair that returns less than this fraction of the original file is treated
# as a deletion, not a fix. Measured need: asked to fix four syntax errors, the
# ensemble once returned a Program.cs at 49% of its original length -- it had
# dropped half the game loop, which parses cleanly and would have been reported
# as a successful build.
SHRINK_FLOOR = 0.75
sys.path.insert(0, SONDER)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import specs  # noqa: E402
import apiextract  # noqa: E402


# The game driver asks an ensemble to generate whole source files.  Unlike an
# ordinary one-off prompt, this can load several large local weights before the
# first compiler invocation.  Keep a little capacity for the desktop, Ollama's
# KV cache, and the compiler rather than treating every currently free byte as
# model memory.
_RAM_RESERVE_GB = 2.0
_VRAM_RESERVE_GB = 1.0


def _local_model_sizes_gb(server, targets: list[tuple[str, str]]) -> tuple[dict[str, float], list[str]]:
    """Return catalogued local model weight sizes and names with no size data."""
    records = {
        name.casefold(): record
        for name, record in server.discovered_model_records()
        if isinstance(record, dict)
    }
    sizes, unknown = {}, []
    for _tier, model in targets:
        if server._is_cloud_model_name(model):
            continue
        record = records.get(str(model).casefold(), {})
        try:
            size = float(record.get("size") or 0) / 1024 ** 3
        except (TypeError, ValueError):
            size = 0.0
        if size > 0:
            sizes[str(model)] = size
        else:
            unknown.append(str(model))
    return sizes, unknown


def _model_headroom_report(sizes_gb: dict[str, float], profile) -> tuple[bool, str]:
    """Conservatively decide whether the selected local weights can be loaded.

    Ollama may split a model across VRAM and RAM.  The guard consequently uses
    the sum of *currently free* memory after fixed host reserves, never total
    installed capacity.  It is intentionally a preflight, not an allocator:
    exact offload and KV-cache use vary by model and context size.
    """
    required = sum(sizes_gb.values())
    ram = max(0.0, float(profile.system_ram_available_gb) - _RAM_RESERVE_GB)
    vram = max(0.0, float(profile.vram_free_gb) - _VRAM_RESERVE_GB)
    usable = ram + vram
    detail = (
        f"selected local weights need about {required:.1f} GiB; "
        f"safe live headroom is {usable:.1f} GiB "
        f"({ram:.1f} GiB RAM + {vram:.1f} GiB VRAM after reserves)"
    )
    return required <= usable, detail


def ensure_model_headroom(server, targets: list[tuple[str, str]], *, allow_low_headroom: bool) -> bool:
    """Print a clear admission decision before an ensemble can load weights."""
    sizes, unknown = _local_model_sizes_gb(server, targets)
    if unknown:
        print("model-size preflight: catalog has no size for " + ", ".join(unknown))
    if not sizes:
        return True
    import system_profile

    profile = system_profile.detect_hardware()
    ok, detail = _model_headroom_report(sizes, profile)
    print("model-size preflight: " + detail)
    if ok or allow_low_headroom:
        if not ok:
            print("WARNING: continuing with low headroom because --allow-low-headroom was supplied")
        return True
    print(
        "refusing to start before loading models: insufficient live headroom. "
        "Close other workloads or use smaller --tiers; pass --allow-low-headroom "
        "only to deliberately stress-test this machine."
    )
    return False


def dependency_brief(name: str) -> str:
    """The ACTUAL public API of this file's already-generated dependencies.

    The first run handed every file the same hand-written contract describing
    what the API was *supposed* to be, and the files still disagreed with each
    other -- 28 member-not-found and 16 assign-to-readonly errors. This reads the
    signatures back out of the generated source instead, so a file is told the
    truth about what it is calling.
    """
    deps = specs.DEPS.get(name, [])
    paths = {d: os.path.join(PROJECT, d) for d in deps
             if os.path.exists(os.path.join(PROJECT, d))}
    if not paths:
        return ""
    return (
        "THE OTHER FILES IN THIS PROJECT ALREADY EXIST. Their real API is below,\n"
        "read directly from the generated source. Call these EXACTLY as written.\n"
        "Do not redefine these types. Do not call a member that is not listed.\n"
        "A member shown as `{ get; }` is READ-ONLY -- you may not assign to it.\n\n"
        + apiextract.api_for(paths) + "\n"
    )


def extract_code(text: str) -> str:
    """Pull a C# source file out of a model response.

    Handles fenced blocks, prose before the first using, and the <think> blocks
    a reasoning model can emit inline.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"^\s*=== ENSEMBLE.*$", "", text, flags=re.M | re.S)
    fenced = re.findall(r"```(?:csharp|cs|c#)?\s*\n(.*?)```", text, flags=re.S)
    if fenced:
        text = max(fenced, key=len)
    idx = text.find("using ")
    if idx > 0:
        text = text[idx:]
    return text.strip() + "\n"


def build() -> tuple[bool, list[str]]:
    """Compile and return (ok, deduplicated error lines)."""
    proc = subprocess.run(
        ["dotnet", "build", "-c", "Release", "--nologo"],
        cwd=PROJECT, capture_output=True, text=True, timeout=1800,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    seen, uniq = set(), []
    for line in out.splitlines():
        if ": error " not in line:
            continue
        line = line.strip()
        if line not in seen:
            seen.add(line)
            uniq.append(line)
    return proc.returncode == 0, uniq


ERROR_RE = re.compile(r"^(?P<path>.+?)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>CS\d+): (?P<msg>.*?)(?: \[|$)")

MISSING_TYPE_RE = re.compile(r"The type or namespace name '(\w+)' could not be found")

# Which namespace supplies a type the model forgot to import. This is
# deterministic build scaffolding, not game logic: a missing `using` says
# nothing about whether the code is right, and asking the model to re-emit a
# whole file to add one import is how files get amputated.
TYPE_NAMESPACES = {
    "Vector2": "System.Numerics", "Vector3": "System.Numerics",
    "Vector4": "System.Numerics", "Matrix4x4": "System.Numerics",
    "Quaternion": "System.Numerics",
    "Color": "Raylib_cs", "Raylib": "Raylib_cs", "KeyboardKey": "Raylib_cs",
    "MouseButton": "Raylib_cs", "Camera3D": "Raylib_cs", "Rectangle": "Raylib_cs",
    "CameraProjection": "Raylib_cs", "Texture2D": "Raylib_cs", "Image": "Raylib_cs",
    "Sound": "Raylib_cs", "Mesh": "Raylib_cs", "Model": "Raylib_cs",
    "ConfigFlags": "Raylib_cs", "MaterialMapIndex": "Raylib_cs",
    "IPEndPoint": "System.Net", "IPAddress": "System.Net", "Dns": "System.Net",
    "UdpClient": "System.Net.Sockets", "SocketException": "System.Net.Sockets",
    "Socket": "System.Net.Sockets",
    "Encoding": "System.Text", "StringBuilder": "System.Text",
    "List": "System.Collections.Generic", "Dictionary": "System.Collections.Generic",
    "IEnumerable": "System.Collections.Generic", "HashSet": "System.Collections.Generic",
    "CultureInfo": "System.Globalization",
    "Math": "System", "MathF": "System", "Action": "System", "Func": "System",
    "Random": "System", "Convert": "System", "Enum": "System",
}


# Wrong-library API calls the model reaches for by habit, and their exact
# equivalent here. Only unambiguous textual swaps belong in this table -- these
# are recall failures the model repeats across runs, and rewriting them
# deterministically costs nothing and cannot delete code. Anything needing the
# expression restructured (Unity's `a.normalized` -> `Vector3.Normalize(a)`)
# is deliberately absent: that needs judgement, so it stays the model's job.
API_SLIPS = [
    (re.compile(r"\bColor\.FromArgb\("), "new Color("),        # System.Drawing
    (re.compile(r"\bVector3\.zero\b"), "Vector3.Zero"),        # Unity casing
    (re.compile(r"\bVector3\.one\b"), "Vector3.One"),
    (re.compile(r"\bVector2\.zero\b"), "Vector2.Zero"),
    (re.compile(r"\bClassId(Assault|Scout|Heavy)\b"), r"ClassId.\1"),  # dropped '.'
    (re.compile(r"\bMatchPhase(Warmup|Live|Ended)\b"), r"MatchPhase.\1"),
    # Generic syntax written with parentheses instead of angle brackets. The
    # model produced `List(Vector3)` three times in one file; it is unambiguous
    # here because no method by these names exists.
    (re.compile(r"\bList\((Vector3|Vector2|Combatant|string|int|float)\)"), r"List<\1>"),
    (re.compile(r"\bnew List<(\w+)>\(\1\(\)\)"), r"new List<\1>()"),
    (re.compile(r"\bnew List\((\w+)\(\)\)"), r"new List<\1>()"),
    # Mixed bracket types: `new List(Vector3>()`. The model opens with a paren
    # and closes with an angle bracket; unambiguous, so safe to rewrite.
    (re.compile(r"\bnew List\((\w+)>\(\)"), r"new List<\1>()"),
    (re.compile(r"\bList<(\w+)\)"), r"List<\1>"),
    (re.compile(r"\bScreen(MainMenu|ClassSelect|Lobby|Match|Scoreboard)\b"), r"Screen.\1"),
]


def fix_api_slips() -> list[str]:
    """Rewrite known wrong-library calls. Returns the files changed."""
    changed = []
    for name, _ in specs.files():
        path = os.path.join(PROJECT, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        fixed, hits = text, 0
        for pattern, repl in API_SLIPS:
            fixed, n = pattern.subn(repl, fixed)
            hits += n
        if hits:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(fixed)
            changed.append(f"{name} ({hits})")
    return changed


NAMESPACE_RE = re.compile(r"^\s*namespace\s+([\w.]+)\s*(\{)?\s*$", re.M)


def align_namespaces() -> list[str]:
    """Put every file in the same namespace as the majority.

    Files are generated independently, so a minority will wrap themselves in a
    namespace the rest never declare -- making those types invisible to
    everything else. Measured: one file of eight declared `namespace
    ArenaShooter` and every other file then failed to find its types. Aligning
    to the majority is a mechanical dedent, so it cannot lose code.
    """
    present = {}
    for name, _ in specs.files():
        path = os.path.join(PROJECT, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        m = NAMESPACE_RE.search(text)
        present[name] = m.group(1) if m else None

    if not present:
        return []
    counts: dict = {}
    for ns in present.values():
        counts[ns] = counts.get(ns, 0) + 1
    majority = max(counts, key=counts.get)
    # Only ever strip a namespace to reach a global majority; adding one would
    # require rewriting every reference, which is not a mechanical change.
    if majority is not None:
        return []

    changed = []
    for name, ns in present.items():
        if ns is None:
            continue
        path = os.path.join(PROJECT, name)
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        out, depth, removed = [], None, False
        for line in lines:
            if not removed and NAMESPACE_RE.match(line):
                removed = True
                depth = 0
                continue
            if removed and depth == 0 and line.strip() == "{":
                depth = 1
                continue
            out.append(line)
        if not removed:
            continue
        # Drop the namespace's closing brace: the last '}' on its own line.
        for i in range(len(out) - 1, -1, -1):
            if out[i].strip() == "}":
                del out[i]
                break
        # Dedent one level so the file reads normally.
        body = "\n".join(ln[4:] if ln.startswith("    ") else ln for ln in out)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body.rstrip() + "\n")
        changed.append(f"{name} (-namespace {ns})")
    return changed


def ensure_usings(errors: list[str]) -> list[str]:
    """Add `using` directives the compiler says are missing.

    Returns the list of files changed. Purely mechanical: it only ever adds an
    import line for a type the compiler explicitly named and that maps to a
    known namespace, and it never touches a line of existing code.
    """
    wanted: dict[str, set[str]] = {}
    for raw in errors:
        m = ERROR_RE.match(raw.strip())
        if not m:
            continue
        t = MISSING_TYPE_RE.search(m.group("msg"))
        if not t:
            continue
        ns = TYPE_NAMESPACES.get(t.group(1))
        if ns:
            wanted.setdefault(os.path.basename(m.group("path")), set()).add(ns)

    changed = []
    for name, namespaces in wanted.items():
        path = os.path.join(PROJECT, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        add = sorted(ns for ns in namespaces if f"using {ns};" not in text)
        if not add:
            continue
        header = "".join(f"using {ns};\n" for ns in add)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header + text)
        changed.append(f"{name} (+{', '.join(add)})")
    return changed


def parse_errors(errors: list[str]):
    """Turn raw MSBuild lines into (filename, line, code, message) tuples."""
    out = []
    for raw in errors:
        m = ERROR_RE.match(raw.strip())
        if not m:
            continue
        out.append((
            os.path.basename(m.group("path")),
            int(m.group("line")),
            m.group("code"),
            m.group("msg").strip(),
        ))
    return out


def repair_lines(server, name: str, parsed, tiers: str) -> bool:
    """Fix syntax errors one source line at a time, splicing the result back.

    Whole-file regeneration is the wrong tool for a token-level slip: asked to
    insert two missing '.' characters, the ensemble returned a file at 44% of
    its original length, and asked to add one missing type annotation it
    returned 13%. Deleting the offending code is always a valid way to make a
    compiler stop complaining, so a whole-file repair loop drifts toward
    deletion. Rewriting exactly one line cannot amputate the file.

    Returns True if any line was changed.
    """
    path = os.path.join(PROJECT, name)
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    # Highest line first, so a rewrite cannot shift the lines still to be fixed.
    targets = sorted({(ln, msg) for f, ln, _code, msg in parsed if f == name},
                     key=lambda t: -t[0])
    changed = False
    for line_no, msg in targets:
        if not (1 <= line_no <= len(lines)):
            continue
        original = lines[line_no - 1]
        lo = max(0, line_no - 4)
        hi = min(len(lines), line_no + 3)
        context = "\n".join(
            ("%4d %s %s" % (i + 1, ">>" if i == line_no - 1 else "  ", lines[i]))
            for i in range(lo, hi)
        )
        prompt = (
            "One line of a C# file has a syntax error. Rewrite ONLY that line.\n\n"
            f"COMPILER SAYS (line {line_no}): {msg}\n\n"
            f"CONTEXT (the broken line is marked >>):\n{context}\n\n"
            f"THE BROKEN LINE:\n{original}\n\n"
            "Output only the single corrected line of C#. Keep the same "
            "indentation. Do not add or remove any other line. Do not explain."
        )
        reply = server.ensemble_answer(prompt, tiers=tiers, num_predict=160, mode="code")
        fixed = extract_code(reply).strip("\n")
        # Strip fences/prose the model may still add, then demand exactly one line.
        candidates = [c for c in fixed.split("\n") if c.strip() and not c.strip().startswith("```")]
        if len(candidates) != 1:
            print(f"    {name}:{line_no}: expected one line, got {len(candidates)} -- skipped", flush=True)
            continue
        new_line = candidates[0]
        if new_line.strip() == original.strip():
            print(f"    {name}:{line_no}: unchanged -- skipped", flush=True)
            continue
        lines[line_no - 1] = new_line
        changed = True
        print(f"    {name}:{line_no}: {original.strip()[:60]}  ->  {new_line.strip()[:60]}", flush=True)

    if changed:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
    return changed


# CS1xxx is the C# parser refusing to read the file. The compiler stops before
# binding, so every semantic error behind it goes unreported -- a file with two
# parse errors can be hiding ninety. A falling total is therefore NOT evidence of
# progress while any parse error remains. This bit me: I read "2 errors" as near
# success when the real count, once it parsed, was 99.
# Matching on code ranges alone is not enough: C# syntax errors are mostly
# CS1xxx but not all of them (CS8180 "{ or ; or => expected" and CS8124
# "Tuple must contain at least two elements" are both the parser). Missing one
# is how a masked count reads as near-success, so this also matches the message
# shapes the parser uses.
# Match on MESSAGE SHAPE only. The earlier version also matched the CS1xxx and
# CS8xxx number ranges, and those ranges leak badly: CS1061 ("does not contain a
# definition"), CS1503 ("cannot convert") and CS1501 ("no overload takes N
# arguments") are BINDER errors that merely live there. Measured on a build with
# zero real parse errors: 101 errors, of which this function claimed 36 were
# parse errors -- 22 CS1061, 13 CS1503, 1 CS1501.
#
# That is not a cosmetic miscount. It pins the masked bit at 1 for every
# realistic candidate, which collapses score = (masked?, total) back into
# ranking by total -- exactly the bug the tuple was introduced to remove. It
# then threw away a candidate that had genuinely fixed the parse error.
#
# codegen_loop.py already matched on shape for precisely this reason. Fixing it
# there and leaving the range version here would be the same single-site fix
# whose recurrence this comment exists to stop.
PARSE_ERROR_RE = re.compile(
    r"(?i)(?:expected|unexpected token|invalid expression|invalid token|"
    r"unterminated|parse error|syntax error|unexpected end of|"
    r"tuple must contain|unclosed|missing closing)"
)


def parse_blocked(errors: list[str]) -> int:
    """How many parse errors are masking the semantic error count."""
    return sum(1 for e in errors if PARSE_ERROR_RE.search(e))


def describe_total(errors: list[str]) -> str:
    """Report a total, flagging when it cannot be trusted."""
    blocked = parse_blocked(errors)
    if blocked:
        return (f"{len(errors)} total (UNRELIABLE: {blocked} parse error(s) are "
                f"masking the semantic count)")
    return f"{len(errors)} total"


def errors_for(errors: list[str], filename: str) -> list[str]:
    stem = filename[:-3]
    return [e for e in errors if filename in e or f"\\{stem}." in e]


def snapshot(name: str) -> None:
    """Keep the last known state of a file before overwriting it."""
    path = os.path.join(PROJECT, name)
    if not os.path.exists(path):
        return
    backup_dir = os.path.join(PROJECT, ".backup")
    os.makedirs(backup_dir, exist_ok=True)
    with open(path, encoding="utf-8") as src:
        text = src.read()
    with open(os.path.join(backup_dir, name), "w", encoding="utf-8", newline="\n") as dst:
        dst.write(text)


def generate(server, name: str, contract: str, tiers: str, num_predict: int,
             use_real_api: bool = False) -> None:
    snapshot(name)
    context = dependency_brief(name) if use_real_api else specs.SHARED_CONTRACT
    prompt = (
        f"{specs.API_BRIEF}\n{context}\n{contract}\n"
        f"Output only the contents of {name}, starting at the first using line."
    )
    t0 = time.time()
    reply = server.ensemble_answer(
        prompt, tiers=tiers, num_predict=num_predict, mode="code"
    )
    code = extract_code(reply)
    with open(os.path.join(PROJECT, name), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(code)
    print(f"    {name}: {len(code)} chars in {time.time() - t0:.0f}s", flush=True)


def repair(server, name: str, contract: str, file_errors: list[str],
           tiers: str, num_predict: int) -> None:
    path = os.path.join(PROJECT, name)
    current = open(path, encoding="utf-8").read()
    prompt = (
        f"{specs.API_BRIEF}\n{specs.SHARED_CONTRACT}\n"
        "The C# file below does not compile. Fix ONLY the listed compiler errors.\n"
        "Keep every part that already works. Output the complete corrected file "
        "and nothing else.\n\n"
        f"ORIGINAL SPEC FOR {name}:\n{contract}\n"
        f"COMPILER ERRORS:\n" + "\n".join(file_errors[:25]) +
        f"\n\nCURRENT {name}:\n{current}"
    )
    t0 = time.time()
    fixed = extract_code(
        server.ensemble_answer(prompt, tiers=tiers, num_predict=num_predict, mode="code")
    )
    if len(fixed) < 40:
        print(f"    {name}: repair returned almost nothing, keeping previous file")
        return

    # Reject an amputation. Asked to fix a syntax error, the model will
    # sometimes delete the offending half of the file instead -- which parses,
    # and would let this loop report BUILD SUCCEEDED on a gutted game. A real
    # fix to a syntax error barely changes the length, so a large shrink is
    # evidence of deletion rather than repair.
    ratio = len(fixed) / max(1, len(current))
    if ratio < SHRINK_FLOOR:
        print(
            f"    {name}: REJECTED repair -- shrank to {ratio:.0%} of the original "
            f"({len(current)} -> {len(fixed)} chars); keeping the previous file",
            flush=True,
        )
        return

    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(fixed)
    verdict = "repaired" if ratio >= 0.95 else f"repaired (now {ratio:.0%} of original)"
    print(f"    {name}: {verdict} in {time.time() - t0:.0f}s", flush=True)


def run_sequential(server, args) -> int:
    """Regenerate in dependency order, each file told its deps' REAL API.

    One file at a time, rebuilding after each, so drift is caught against the
    code that actually exists rather than against an idealised contract. A file
    gets `attempts` tries; if it still has errors we keep the best version (the
    one with the fewest) and move on, because a later file may be the real cause.
    """
    contracts = dict(specs.files())
    order = [n for n, _ in specs.files()]
    if args.only:
        order = [n for n in order if n == args.only]

    for name in order:
        # Seed the tracker with the file ALREADY on disk. Regeneration is not
        # monotonic: asked to rewrite a ClassKit.cs that had zero errors, the
        # ensemble returned one with six (a dropped '.' and four calls to
        # System.Drawing's Color.FromArgb). A loop that only compares its own
        # attempts to each other will happily replace working code with worse.
        path = os.path.join(PROJECT, name)
        best_code, best_errors, best_total = None, None, None
        if os.path.exists(path):
            _ok, incumbent_errors = build()
            with open(path, encoding="utf-8") as fh:
                best_code = fh.read()
            best_errors = errors_for(incumbent_errors, name)
            best_total = (1 if parse_blocked(incumbent_errors) else 0,
                          len(incumbent_errors))
            print(f"\n=== {name}: incumbent has {len(best_errors)} error(s), "
                  f"{describe_total(incumbent_errors)} ===", flush=True)
            if not best_errors:
                print(f"    {name}: already CLEAN, not regenerating", flush=True)
                continue

        for attempt in range(1, args.attempts + 1):
            print(f"\n=== {name} (attempt {attempt}/{args.attempts}) ===", flush=True)
            generate(server, name, contracts[name], args.tiers, args.num_predict,
                     use_real_api=True)

            slips = fix_api_slips()
            if slips:
                print(f"    rewrote wrong-library calls: {'; '.join(slips)}", flush=True)
            # A regenerated file will re-wrap itself in a namespace the other
            # seven never declare, hiding its types from all of them. Six of the
            # seven remaining errors were exactly that.
            aligned = align_namespaces()
            if aligned:
                print(f"    aligned namespaces: {'; '.join(aligned)}", flush=True)
            ok, errors = build()
            added = ensure_usings(errors)
            if added:
                print(f"    added usings: {'; '.join(added)}", flush=True)
                ok, errors = build()

            mine = errors_for(errors, name)
            print(f"    {name}: {len(mine)} error(s) in this file, {describe_total(errors)}", flush=True)
            for e in mine[:5]:
                print("      " + e.split("): ", 1)[-1][:120], flush=True)

            with open(os.path.join(PROJECT, name), encoding="utf-8") as fh:
                code = fh.read()
            # Score on TOTAL errors, not this file's. A file's public API is
            # what every dependent compiles against, so a version with MORE
            # local errors can still be the right one: regenerating GameMap.cs
            # took its own count 10 -> 14 while taking the project 106 -> 14,
            # because the rest of the code could finally resolve against it.
            # Score as (still-unparseable?, total). A version with parse errors
            # always loses to one without, however small its total looks: its
            # count is masking everything the binder never got to.
            score = (1 if parse_blocked(errors) else 0, len(errors))
            if best_total is None or score < best_total:
                best_code, best_errors, best_total = code, mine, score
            if not mine:
                print(f"    {name}: CLEAN", flush=True)
                break

        # Restore the best attempt if the last one was worse.
        if best_code is not None and best_errors is not None:
            with open(os.path.join(PROJECT, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(best_code)
            if best_errors:
                print(f"    {name}: keeping best attempt ({len(best_errors)} error(s))", flush=True)

    ok, errors = build()
    print(f"\n=== FINAL: {'BUILD SUCCEEDED' if ok else f'{len(errors)} error(s)'} ===")
    if not ok:
        counts: dict[str, int] = {}
        for n, _ in specs.files():
            c = len(errors_for(errors, n))
            if c:
                counts[n] = c
        for n, c in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"   {c:4d}  {n}")
    return 0 if ok else 1


def main() -> int:
    global PROJECT
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--project",
        default=PROJECT,
        help="output project directory (default: SONDER_GAME_PROJECT or a sibling directory)",
    )
    ap.add_argument("--tiers", default="code,reasoning")
    ap.add_argument(
        "--allow-low-headroom", action="store_true",
        help="allow a local ensemble whose catalogued weights exceed safe live RAM/VRAM headroom",
    )
    ap.add_argument("--repair-rounds", type=int, default=6)
    ap.add_argument("--num-predict", type=int, default=3000)
    ap.add_argument("--only", default="")
    ap.add_argument("--repair-only", action="store_true")
    ap.add_argument(
        "--sequential", action="store_true",
        help="regenerate in dependency order, feeding each file the REAL "
             "extracted API of its dependencies instead of a hand-written contract",
    )
    ap.add_argument("--attempts", type=int, default=2,
                    help="tries per file in sequential mode; best result is kept")
    ap.add_argument(
        "--resume", action="store_true",
        help="skip files already on disk (a generation run costs minutes per "
             "file, so an interrupted run should not start over)",
    )
    args = ap.parse_args()
    PROJECT = os.path.abspath(os.path.expanduser(args.project))

    # A typo here used to select zero files in sequential mode.  The driver
    # would then build whatever happened to already be in --project and could
    # print BUILD SUCCEEDED without asking Sonder to write the requested file.
    # Validate before importing server as well: an invalid command must be
    # cheap and must not need a running local model service to explain itself.
    known_files = {name for name, _contract in specs.files()}
    if args.only and args.only not in known_files:
        print("no such file in specs: %s" % args.only)
        print("available files: %s" % ", ".join(sorted(known_files)))
        return 2

    import server  # late import so the sys.path insert applies

    # Resolve and admit the exact ensemble once, before either generation mode
    # can cause Ollama to load a model.  In particular, sequential mode used to
    # bypass this visibility entirely despite issuing the same ensemble calls.
    targets, unknown = server._ensemble_targets(args.tiers)
    if not ensure_model_headroom(
        server, targets, allow_low_headroom=args.allow_low_headroom,
    ):
        return 2

    if args.sequential:
        return run_sequential(server, args)

    file_list = specs.files()
    if args.only:
        file_list = [(n, c) for n, c in file_list if n == args.only]

    print("ensemble models: " + ", ".join(f"{t}={m}" for t, m in targets))
    if unknown:
        print(f"unknown tiers ignored: {unknown}")
    print(f"files: {', '.join(n for n, _ in file_list)}\n")

    if not args.repair_only:
        print("=== GENERATION ===", flush=True)
        for name, contract in file_list:
            if args.resume and os.path.exists(os.path.join(PROJECT, name)):
                print(f"    {name}: already present, skipping", flush=True)
                continue
            generate(server, name, contract, args.tiers, args.num_predict)

    print("\n=== BUILD / REPAIR ===", flush=True)
    contracts = dict(specs.files())
    for attempt in range(args.repair_rounds + 1):
        ok, errors = build()
        if ok:
            print(f"\nBUILD SUCCEEDED after {attempt} repair round(s)")
            return 0
        print(f"\n--- round {attempt}: {len(errors)} error(s) ---", flush=True)
        for e in errors[:12]:
            print("   " + e[:200], flush=True)
        if attempt == args.repair_rounds:
            break

        # Mechanical imports first: they are safe, free, and usually account for
        # most of a round's errors, so fixing them keeps the model's attention
        # on defects that actually need judgement.
        added = ensure_usings(errors)
        if added:
            print(f"   added usings: {'; '.join(added)}", flush=True)
            continue

        # Repair every file that has errors this round, worst first. Repairing
        # only one file per round wastes a full generation cycle when several
        # files are independently broken.
        counts = {n: len(errors_for(errors, n)) for n, _ in specs.files()}
        broken = sorted([n for n, c in counts.items() if c], key=lambda n: -counts[n])
        if not broken:
            print("   no error line named a known file; cannot target a repair")
            break
        print(f"   repairing: {', '.join(f'{n}({counts[n]})' for n in broken)}", flush=True)

        parsed = parse_errors(errors)
        # CS1xxx is the parser complaining; those are almost always a dropped
        # character on one line, and a line-targeted rewrite cannot amputate the
        # file. Anything else is a binding error that may genuinely need the
        # whole file reconsidered.
        syntax_files = {f for f, _ln, code, _m in parsed if code.startswith("CS1")}
        for name in broken:
            if name in syntax_files:
                if repair_lines(server, name, parsed, args.tiers):
                    continue
                print(f"    {name}: line repair changed nothing, falling back to whole file", flush=True)
            repair(server, name, contracts[name], errors_for(errors, name),
                   args.tiers, args.num_predict)

    print("\nBUILD STILL FAILING after all repair rounds")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
