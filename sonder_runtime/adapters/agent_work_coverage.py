"""Whether an agent's validators and verifiers covered the work it changed.

Completion claims rest on validators that touched the changed disk state and
verifiers that examined the work in scope, not on any exit-0 command the
caller chose. This module owns the mutation records, the path normalization
and containment, the no-op flag and build-driver tables, and the coverage
predicates. It resolves paths through the filesystem adapter and parses
patches through the text-patch adapter, so it lives with the adapters. Moved
from ``server.py`` in the WP1 Three-Hundred-Twenty-Seventh Slice with its
behaviour byte-for-byte intact.
"""
from __future__ import annotations

import os
import re

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.filesystem.text_patch as text_patch_ops
from sonder_runtime.domain.agents.activity_command import agent_argv, batch_operations


# Flags that make a program report on itself instead of on the project. Taken
# from the set ``validation_covers`` already applies to ``workspace_run``
# -- the sibling free-form-argv tool -- so the two routes refuse the same
# no-ops rather than each keeping a private list.
NO_OP_COMMAND_FLAGS = frozenset({
    "--help", "-h", "--version", "-version", "--collect-only", "--co",
    "--list-tests", "--dry-run", "--fixtures", "--fixtures-per-test",
    "--show-only", "-n",
})


# Programs that build or test a project, and the action words that mean they
# did. Derived, not recalled: the first rows are exactly the argv
# ``harness_tools.build_run`` auto-detects from a root's own marker files
# (Makefile, Cargo.toml, CMakeLists.txt, go.mod, package.json, build.gradle,
# pom.xml), and the rest are the drivers ``validation_covers`` already
# treats as broad for ``workspace_run``. An empty tuple means any non-no-op
# invocation of that program builds something.
BUILD_DRIVERS = {
    "make": (), "gmake": (), "nmake": (), "mingw32-make": (),
    "cargo": ("build", "check", "test"),
    "cmake": ("--build",),
    "go": ("build", "test", "vet", "install"),
    "npm": ("build", "test", "check", "lint"),
    "pnpm": ("build", "test", "check", "lint"),
    "yarn": ("build", "test", "check", "lint"),
    "gradle": ("build", "test", "check", "assemble", "verify"),
    "gradlew": ("build", "test", "check", "assemble", "verify"),
    "mvn": ("package", "install", "verify", "test", "compile"),
    "msbuild": (), "ninja": (), "ctest": (), "pytest": (),
    "dotnet": ("build", "test"),
}


def normalized_path(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(str(file_ops.resolve_path(text)))
    except (OSError, PermissionError, ValueError):
        return os.path.normcase(os.path.abspath(text))


def path_within(path, root):
    path = normalized_path(path)
    root = normalized_path(root)
    if not path or not root:
        return False
    try:
        return os.path.commonpath((path, root)) == root
    except (OSError, ValueError):
        return False


def explicit_command_paths(argv, cwd):
    """Resolve path-looking argv entries against the validator working dir."""
    resolved = []
    for item in argv:
        text = str(item or "").strip()
        if not text or text.startswith("-"):
            continue
        looks_pathlike = (
            os.path.isabs(text)
            or bool(re.match(r"^[A-Za-z]:[\\/]", text))
            or "/" in text
            or "\\" in text
            or text in {".", ".."}
            or bool(os.path.splitext(text)[1])
        )
        if not looks_pathlike:
            continue
        candidate = text if os.path.isabs(text) else os.path.join(cwd, text)
        resolved.append(normalized_path(candidate))
    return [path for path in resolved if path]


def paths_covered_by_targets(paths, targets):
    return bool(paths and targets) and all(
        any(path == target or path_within(path, target) for target in targets)
        for path in paths
    )


def build_command_examines(command, root, scope, changed):
    """Whether a ``build_run`` command could have looked at the work at all.

    ``build_run`` is the one verifier with neither a ``path`` nor a fixed
    program: ``harness_tools.build_run`` takes ``root``/``command``/``timeout``
    and appends nothing, so the S2 narrowing that made this gate read ``path``
    cannot reach it and ``root`` alone decided coverage. Measured on a project
    with no build system, ``build_run(root=proj, command="git --version")``
    returns ``ok=True, returncode=0``, so ``verification_ok`` was granted --
    and through ``_work_validated`` that satisfied a whole ``validate`` task --
    for a command that examined nothing. "Exit 0" is not a verdict about the
    work when the caller chose the whole argv.

    The control is not new doctrine: ``validation_covers`` already
    applies it to ``workspace_run``, the sibling tool whose argv the caller
    also chooses, on the *validation* route. ``build_run`` reached the
    *verification* route, where the same shape had no check. The command is
    tokenized with ``str.split()`` because that is exactly what the child does
    (``harness_tools.build_run``'s ``parts = command.split()``); analysing it
    any other way would judge an argv the child never runs.

    An empty ``command`` stays covered. The argv is then derived from the
    root's own build files, which the caller cannot forge, and a root with no
    build system comes back ``{"ok": False, ...}`` so ``tool_ok`` already
    refuses it one level up. That is the non-fabricable binding this check
    exists to demand, and it is already present on that path.
    """
    text = str(command or "").strip()
    if not text:
        return True
    parts = text.split()
    if not parts:
        return True
    program = os.path.basename(parts[0]).casefold()
    for suffix in (".exe", ".cmd", ".bat"):
        if program.endswith(suffix):
            program = program[: -len(suffix)]
            break
    argv = parts[1:]
    lowered = [item.casefold() for item in argv]

    # Self-reporting flags first, and before the driver table: ``make
    # --version`` is the project's real build program and still builds nothing.
    if any(item.split("=", 1)[0] in NO_OP_COMMAND_FLAGS for item in lowered):
        return False
    if any(
        item == "clean" or item.endswith(":clean") or item in {"/t:clean", "-t:clean"}
        for item in lowered
    ):
        return False
    # Inline source runs the caller's own text, never the project's.
    if program in {"python", "py", "python3", "node"} and any(
        flag in lowered for flag in ("-c", "-e", "--eval", "-p", "--print")
    ):
        return False

    if program in BUILD_DRIVERS:
        required = BUILD_DRIVERS[program]
        if not required or any(action in lowered for action in required):
            return True

    # Otherwise it has to say what it looked at, and that has to be the work.
    targets = explicit_command_paths(argv, root)
    targets = [target for target in targets if path_within(target, scope)]
    if not targets:
        return False
    if not changed:
        return True
    return paths_covered_by_targets(changed, targets)


def verification_covers(tool_name, args, mutations, project_scope=""):
    """Whether this verifier ran over the work this run is answerable for.

    Keyed on ``root`` NARROWED BY ``path``. This used to key on ``root`` alone,
    justified here as: *"their `path` argument narrows which checks run inside
    it, not what those checks exercise."* That was refuted by code in the same
    lane. ``harness_tools`` appends ``path`` straight to the child argv
    (``cmd.append(path)`` in test_run/lint_run/format_code/typecheck_run), so
    ``path`` decides what the child actually looks at -- it *is* what those
    checks exercise. ``server.py:15659`` says so in the other direction, and
    until the confinement added beside this fix, ``path`` could even point
    outside ``root`` altogether (measured: ``test_run`` executed a file outside
    the authorized root through it).

    The half the old comment had right is kept, and is why the narrowing is
    conditional: ``path`` is empty on a default invocation, and reading it
    unconditionally -- as the file-oriented ``validation_covers`` does --
    would answer "" for nearly every real call and refuse verifications that
    genuinely covered the change. So an empty ``path`` means "the whole root",
    exactly as before; a non-empty one means the verifier looked at that
    subtree and must be judged on it.

    Without this, the model changes ``payments.py`` and runs the verifier
    narrowed to ``tests/`` -- a real, passing, in-scope check of a different
    part of the tree -- and it counted as covering the change.

    The no-mutation case is decided explicitly, never left to fall through an
    empty ``all()``: ``all([])`` is True, so a run that changed nothing
    previously reported *any* root as covering -- a check answering yes because
    it had nothing to check. That is load-bearing now that a passing verifier
    also sets validation_attempted/validation_passed, which is what
    _task_passed and _completion_gate accept for a whole ``validate`` task.
    With nothing changed on disk, the work the run is answerable for is the
    scope it was confined to, so the verifier has to cover that scope.

    An unscoped run has no declared boundary to violate -- ``root`` defaults to
    the server CWD, which is that run's implicit scope -- so it is covered.
    That default is a separately-tracked item, not something decided here.
    """
    args = args if isinstance(args, dict) else {}
    root = str(args.get("root") or ".")
    scope = normalized_path(root)
    if not scope:
        return False
    # A non-empty `path` narrows the scope to what the child was actually
    # pointed at. Resolved against `root`, matching how the child resolves it
    # (harness_tools runs it with cwd=root).
    narrowing = str(args.get("path") or "").strip()
    if narrowing:
        narrowed = normalized_path(
            narrowing if os.path.isabs(narrowing)
            else os.path.join(root, narrowing)
        )
        # Only ever narrows. A `path` that resolves outside `root` is refused
        # by harness_tools now; if one still arrives, the scope it covers is
        # not the root it claimed, so fall closed rather than widen.
        if not narrowed or not path_within(narrowed, scope):
            return False
        scope = narrowed
    changed = [
        str(record.get("path") or "") for record in mutations
        if record.get("path")
    ]
    # ``build_run`` has no ``path`` to narrow with, so the narrowing above can
    # never reach it and ``root`` alone said yes to any command that exited 0.
    # See ``build_command_examines``: the caller chooses this whole argv,
    # so the argv has to name something in scope before its exit status counts.
    if tool_name == "build_run" and not build_command_examines(
        args.get("command"), root, scope, changed,
    ):
        return False
    if not changed:
        declared = normalized_path(project_scope)
        return path_within(declared, scope) if declared else True
    return all(path_within(path, scope) for path in changed)


def mutation_records(tool_name, args):
    args = args if isinstance(args, dict) else {}
    if tool_name == "file_batch_write":
        operations = batch_operations(args) or []
        return [
            {"tool": tool_name, "path": normalized_path(item.get("path", ""))}
            for item in operations if isinstance(item, dict)
        ]
    if tool_name == "text_patch":
        try:
            root = args.get("root") or "."
            return [
                {"tool": tool_name, "path": normalized_path(os.path.join(root, *item["path"].split("/")))}
                for item in text_patch_ops._parse(args.get("patch", ""))
            ]
        except (TypeError, ValueError, PermissionError):
            return [{"tool": tool_name, "path": normalized_path(args.get("root", ""))}]
    if tool_name == "data_convert":
        if args.get("apply") is not True:
            return []
        return [{
            "tool": tool_name,
            "path": normalized_path(args.get("output_path", "")),
        }]
    if tool_name == "archive_create":
        root = str(args.get("root") or ".")
        destination = str(args.get("destination") or "")
        if destination and not os.path.isabs(destination):
            destination = os.path.join(root, destination)
        return [{"tool": tool_name, "path": normalized_path(destination)}]
    path = args.get("path", "")
    if tool_name == "archive_extract":
        path = args.get("destination", "")
    elif tool_name == "artifact_generate":
        path = args.get("output_dir") or os.path.join(
            "artifacts", "generated", str(args.get("name", "generated-artifact")),
        )
    elif tool_name in {"game_generate_and_test", "game_generation_campaign"}:
        path = os.path.join("games", str(args.get("name", "generated-game")))
    elif tool_name in {"file_copy", "file_move"}:
        path = args.get("destination", "")
    record = {
        "tool": tool_name,
        "path": normalized_path(path),
    }
    if tool_name == "file_move":
        record["source"] = normalized_path(args.get("source", ""))
    return [record]


def validation_covers(tool_name, args, mutations, observation=""):
    """Require validators to touch changed disk state, not equivalent draft code."""
    args = args if isinstance(args, dict) else {}
    records = [record for record in mutations if record.get("tool")]
    if not records:
        if tool_name in {"artifact_verify", "artifact_ground"}:
            return bool(str(args.get("path") or "").strip())
        if tool_name == "ground_artifact":
            checks = args.get("checks_json", args.get("checks", []))
            return bool(str(args.get("artifact") or "") and checks)
        if tool_name in {
            "game_reference_suite", "self_heal_check", "memory_quality_report",
            "memory_privacy_review", "learning_health_status",
        }:
            return True
    paths = [record["path"] for record in records if record.get("path")]
    target = normalized_path(args.get("path", args.get("artifact", "")))

    if tool_name == "archive_extract":
        destination = normalized_path(args.get("destination", ""))
        return bool(destination) and all(
            record["tool"] == "archive_extract"
            and record.get("path") == destination
            for record in records
        ) and '"validation_passed": true' in str(observation or "").lower()

    if tool_name == "archive_create":
        root = str(args.get("root") or ".")
        destination = str(args.get("destination") or "")
        if destination and not os.path.isabs(destination):
            destination = os.path.join(root, destination)
        destination = normalized_path(destination)
        return bool(destination) and all(
            record["tool"] == "archive_create"
            and record.get("path") == destination
            for record in records
        ) and '"ok": true' in str(observation or "").lower() and bool(
            re.search(r'"archive_sha256":\s*"[0-9a-f]{64}"', str(observation or "").lower())
        )

    if tool_name in {
        "game_reference_suite", "game_generate_and_test", "game_generation_campaign",
    }:
        game_path = normalized_path(
            os.path.join("games", str(args.get("name", "generated-game")))
        )
        return bool(records) and all(
            record["tool"] in {
                "game_generate_and_test", "game_generation_campaign",
            }
            and record.get("path") == game_path
            for record in records
        )
    if tool_name == "memory_quality_report":
        return bool(records) and all(
            record["tool"] == "memory_quality_repair" for record in records
        )
    if tool_name == "memory_privacy_review":
        return bool(records) and all(
            record["tool"] == "memory_privacy_repair" for record in records
        )
    if tool_name == "learning_health_status":
        return bool(records) and all(record["tool"] in {
            "memory_embedding_backfill",
            "memory_interaction_embedding_backfill",
        } for record in records)
    if tool_name in {"artifact_verify", "artifact_ground"}:
        return bool(records) and all(
            record["tool"] == "artifact_generate"
            and bool(target)
            and path_within(target, record.get("path", ""))
            for record in records
        )
    if tool_name == "script_run":
        if target and paths and all(path == target for path in paths):
            return True
        name = os.path.basename(target).lower()
        if not any(
            word in name for word in ("test", "check", "verify", "smoke", "build")
        ):
            return False
        cwd = normalized_path(
            args.get("cwd") or os.path.dirname(target)
        )
        if not paths:
            return bool(cwd)
        return bool(cwd) and all(
            path_within(path, cwd) for path in paths
        )
    if tool_name == "workspace_run":
        program = os.path.basename(str(args.get("program", ""))).lower()
        argv = agent_argv(args)
        argv_text = [item.casefold() for item in argv]
        no_op_flags = {
            "--help", "-h", "--version", "--collect-only", "--co",
            "--list-tests", "--dry-run", "--fixtures", "--fixtures-per-test",
            "--show-only",
        }
        if any(item.split("=", 1)[0] in no_op_flags for item in argv_text):
            return False
        if program in {"ctest", "ctest.exe", "ninja", "ninja.exe"} and "-n" in argv_text:
            return False
        if "help" in argv_text and program in {
            "cmake", "cmake.exe", "ninja", "ninja.exe", "gradle", "gradle.bat",
            "mvn", "mvn.cmd", "npm", "npm.cmd", "cargo", "cargo.exe",
        }:
            return False
        clean_only = any(
            item == "clean"
            or item.endswith(":clean")
            or item in {"/t:clean", "-t:clean"}
            for item in argv_text
        )
        if clean_only:
            return False
        observation_lower = str(observation or "").casefold()
        if re.search(
            r"(?:no tests ran|collected\s+0\s+items|total tests:\s*0|"
            r"(?<!\d)0\s+tests\s+(?:passed|run)\b)",
            observation_lower,
        ):
            return False
        cwd = normalized_path(args.get("cwd") or ".")
        explicit_targets = explicit_command_paths(argv, cwd)
        explicit_coverage = paths_covered_by_targets(
            paths, explicit_targets,
        )
        if not paths:
            explicit_coverage = bool(explicit_targets)

        python_programs = {"python", "python.exe", "py", "py.exe"}
        node_programs = {"node", "node.exe"}
        if program in python_programs and any(
            flag in argv_text for flag in ("-c", "-command")
        ):
            return False
        if program in node_programs and any(
            flag in argv_text for flag in ("-e", "--eval", "-p", "--print")
        ):
            return False

        broad = program in {
            "pytest", "pytest.exe", "ctest", "ctest.exe", "ninja", "ninja.exe",
            "msbuild", "msbuild.exe",
        }
        if program in {"cmake", "cmake.exe"}:
            broad = "--build" in argv_text
        elif program in {"cargo", "cargo.exe"}:
            broad = any(action in argv_text for action in ("test", "check", "build"))
        elif program in {"dotnet", "dotnet.exe"}:
            broad = any(action in argv_text for action in ("test", "build"))
        elif program in {"npm", "npm.cmd"}:
            broad = (
                "test" in argv_text
                or (
                    "run" in argv_text
                    and any(action in argv_text for action in ("build", "check", "lint"))
                )
            )
        elif program in {"gradle", "gradle.bat", "mvn", "mvn.cmd"}:
            broad = any(
                action in argv_text
                for action in ("test", "check", "build", "verify", "package")
            )
        elif program in {"flutter", "flutter.bat", "dart", "dart.exe"}:
            broad = any(
                action in argv_text
                for action in ("test", "analyze", "build", "compile")
            )
        elif program in python_programs and "-m" in argv_text:
            module_index = argv_text.index("-m") + 1
            module = argv_text[module_index] if module_index < len(argv_text) else ""
            broad = module in {"pytest", "unittest"}
            if module in {"py_compile", "compileall"}:
                return explicit_coverage
        elif program in node_programs:
            broad = "--test" in argv_text

        if broad:
            return bool(cwd) and (
                not paths
                or all(path_within(path, cwd) for path in paths)
            )
        if program in (
            python_programs
            | node_programs
            | {"cl", "cl.exe", "g++", "g++.exe", "clang++", "clang++.exe"}
        ):
            return explicit_coverage
        return False
    if tool_name == "image_inspect":
        return bool(
            target in paths
            and os.path.splitext(target)[1].lower()
            in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ppm", ".svg"}
        )
    if tool_name in {"file_read", "file_read_range"}:
        return bool(
            target in paths
            and os.path.splitext(target)[1].lower()
            in {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".toml", ".xml"}
        )
    if tool_name in {"workspace_inventory", "directory_tree", "file_find", "text_search"}:
        root = normalized_path(args.get("root", args.get("path", ".")))
        observed = os.path.normcase(str(observation or ""))
        eligible = [
            record["path"] for record in records
            if record.get("path")
            and (
                (
                    tool_name in {"workspace_inventory", "directory_tree", "file_find"}
                    and record["tool"] in {
                        "directory_create", "file_copy", "file_move",
                    }
                )
                or (
                    tool_name == "text_search"
                    and os.path.splitext(record["path"])[1].lower()
                    in {".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".toml", ".xml"}
                )
            )
        ]
        return bool(eligible) and all(
            (path.startswith(root + os.sep) or path == root)
            and os.path.basename(path) in observed
            for path in eligible
        )
    # run_code/run_project validate generated snippets or temp projects, not the
    # persistent files just edited. self_heal_check is likewise unrelated.
    return False
