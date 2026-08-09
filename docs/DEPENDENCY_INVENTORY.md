# Dependency inventory

`dependency_inventory` is a read-only MCP and agent inspection tool for a
bounded workspace tree. It parses declared dependencies and lockfile-resolved
versions without invoking a package manager, importing project code, or using
the network.

```json
{"path":".","max_depth":5,"max_files":100,"max_total_bytes":2000000,"max_results":2000}
```

Each item includes `ecosystem`, `name`, `version`, `kind` (`declared` or
`resolved`), `scope`, and a workspace-relative `evidence` path. Parse failures
are reported independently in `errors`; valid neighboring manifests still
produce results. Output and errors are sorted deterministically.

Supported inputs include Python `pyproject.toml`, `setup.cfg`, Pipfile, requirements,
Poetry/Pipfile/uv locks; Node package/npm/Yarn/pnpm files; Cargo, Go modules,
.NET project/central-package/lock files; Maven POMs; Gradle build and lock
files; and Dart pubspec manifests and locks.

The tool refuses symlinks and junctions, sensitive/control-state trees, and
paths outside configured roots. Hard ceilings apply even when callers request
larger budgets: depth 8, 200 manifest files, 512,000 bytes per file, 4,000,000
total bytes, 25,000 scanned entries, and 5,000 returned items. It reports
truncation explicitly and does not infer build commands or frameworks; use the
existing workspace/project detection surfaces for those questions.
