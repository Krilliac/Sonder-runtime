"""Shared fake HostProbes for host tool discovery tests (not a test module)."""
from __future__ import annotations

import ntpath
import posixpath

from sonder_runtime.adapters.host_tools.bounded_process import BoundedRun
from sonder_runtime.adapters.host_tools.probes import HostProbes


class FakeHost:
    """In-memory host: files, dirs, registry values and scripted runs."""

    def __init__(self, system="Linux", *, path="", env=None, home="/home/alice", user="alice"):
        self.system = system
        self.files: dict[str, str] = {}  # path -> identity
        self.contents: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.outputs: dict[tuple, BoundedRun] = {}
        self.runs: list[tuple] = []
        self.envs: list[dict] = []
        self.local: set[str] = set()
        self.registry_values: dict[tuple[str, str, str], str] = {}
        self.registry_keys: dict[tuple[str, str], tuple[str, ...]] = {}
        self.realpaths: dict[str, str] = {}
        base_env = {"PATH": path}
        base_env.update(env or {})
        self.env = base_env
        self.home = home
        self.user = user

    @property
    def _path(self):
        return ntpath if self.system == "Windows" else posixpath

    def add_exe(self, path, identity="1:1", version_output=None, outcome="ok"):
        self.files[path] = identity
        self.dirs.add(self._path.dirname(path))
        if version_output is not None:
            self.outputs[path] = BoundedRun(outcome, version_output, 0 if outcome == "ok" else 1, 5)
        return path

    def which(self, name, directory):
        candidates = [name]
        if self.system == "Windows" and not name.lower().endswith((".exe", ".cmd", ".bat")):
            candidates = [name + ".exe", name + ".cmd", name + ".bat"]
        for candidate in candidates:
            full = self._path.join(directory, candidate)
            if full in self.files:
                return full
        return None

    def run(self, argv, timeout, env, **kwargs):
        self.runs.append(tuple(argv))
        self.envs.append(dict(env))
        key = tuple(argv)
        if key in self.outputs:
            return self.outputs[key]
        if argv[0] in self.outputs:
            return self.outputs[argv[0]]
        return BoundedRun("start_failed", "", None, 0)

    def list_dir(self, path, limit):
        prefix = path.rstrip("/\\")
        sep = "\\" if self.system == "Windows" else "/"
        names = set()
        for item in list(self.dirs) + list(self.files):
            if item.startswith(prefix + sep):
                names.add(item[len(prefix) + 1:].split(sep)[0])
        return tuple(sorted(names))[:limit]

    def probes(self):
        class Registry:
            def __init__(inner):
                pass

            def subkeys(inner, hive, path, *, limit):
                return self.registry_keys.get((hive, path), ())[:limit]

            def value(inner, hive, path, name):
                return self.registry_values.get((hive, path, name))

        registry = Registry() if (self.registry_values or self.registry_keys) else None
        return HostProbes(
            system=self.system,
            env=self.env,
            home=self.home,
            user=self.user,
            which=self.which,
            is_file=lambda p: p in self.files,
            is_dir=lambda p: p in self.dirs,
            list_dir=self.list_dir,
            stat_identity=lambda p: self.files.get(p),
            read_small=lambda p, limit: self.contents.get(p),
            run=self.run,
            registry=registry,
            project_local=lambda p: any(p.startswith(root) for root in self.local),
            realpath=lambda p: self.realpaths.get(p, p),
            release="test",
            machine="x86_64",
        )
