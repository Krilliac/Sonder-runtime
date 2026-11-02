"""Entry point of the recorder subprocess: ``python -I recorder_worker.py job.json result.json``.

Stdlib only; it never imports ``sonder_runtime``.  It loads the pure ``domain/meshgen/dsl`` package by file
path, records the script named in the job file and writes one JSON result file:

* ``{"ok": true, "text": <ops.json text>}``
* ``{"ok": false, "line": N, "message": "..."}`` for a ``ScriptError``

The parent (``record_runner``) enforces a hard wall-clock kill and a memory ceiling around this process, so a
script that makes a single builtin do too much work cannot stall or exhaust the server.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys


def _load_dsl():
    """Load ``domain/meshgen`` by path as the package ``_meshgen`` (its modules use relative imports)."""
    here = os.path.dirname(os.path.abspath(__file__))
    folder = os.path.join(os.path.dirname(os.path.dirname(here)), "domain", "meshgen")
    spec = importlib.util.spec_from_file_location(
        "_meshgen", os.path.join(folder, "__init__.py"), submodule_search_locations=[folder])
    package = importlib.util.module_from_spec(spec)
    sys.modules["_meshgen"] = package
    spec.loader.exec_module(package)
    return (importlib.import_module("_meshgen.dsl.recorder"), importlib.import_module("_meshgen.dsl.manifest"),
            importlib.import_module("_meshgen.dsl.errors"))


def main(argv):
    job_path, result_path = argv[1], argv[2]
    with open(job_path, "r", encoding="utf-8") as handle:
        job = json.load(handle)
    recorder, manifest_module, errors = _load_dsl()
    manifest = None
    if job.get("kit_source") is not None and job.get("kit_module"):
        manifest = manifest_module.build_manifest(job["kit_source"], job["kit_module"])
    try:
        recording = recorder.compile_script(job["source"], manifest, seed=int(job.get("seed") or 0))
        result = {"ok": True, "text": recording.text}
    except errors.ScriptError as exc:
        result = {"ok": False, "line": int(exc.line), "message": str(exc.message)[:1000]}
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
