"""file_ops guards for code the runtime imports, reads it holds secret, and bounded reads.

Each test pins one reviewed hole:

* Only ``tests/conftest.py`` was protected, yet the pytest gates collect
  ``testpaths = tests proposals``: they import every test module and nested
  conftest there and run ``test*.txt`` files as doctests, and pytest's config
  (any name pytest 9 discovers beside the root, or a nearer one inside
  ``tests``) decides what is collected and loaded.
* The mutation guard left the runtime's interpreter environment (``venv``,
  site-packages, ``.pth``/``sitecustomize.py``) and new or existing top-level
  packages under the install root (``sys.path[0]``) writable with no token.
* The ``__init__`` check was case-sensitive and ``.pyw`` was not a module
  suffix, though Windows imports both; ``archive_extract`` checked only its
  destination directory, so members could plant either kind of module.
* ``resolve_repository_read_path`` resolved the personal corpus that the
  direct read tools refuse.
* ``read_file`` read the whole file into memory before applying its cap.
* The SQLite preview built its URI from the raw path, so '#' or '%' in a file
  name opened a different database (and could cut off ``mode=ro``).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.inspection import archive_tools

PAYLOAD = "import os\nos.system('payload')\n"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: ws)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.delenv("SONDER_FILE_APPROVAL_CODE", raising=False)
    return ws, home


@pytest.mark.parametrize("relative", [
    "tests/test_zz_planted.py",
    "tests/sub/conftest.py",
    "tests/unit/helpers.py",
    "proposals/persona_pack/test_persona_pack.py",
    "pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
    "pytest.toml",
    ".pytest.toml",
    ".pytest.ini",
    "PYTEST.TOML",
    "tests/pytest.ini",
    "tests/setup.cfg",
    "tests/unit/tox.ini",
    "proposals/pytest.toml",
    "scripts/.pytest.ini",
    "tests/test_doctest_planted.txt",
    "tests/sub/test_notes.rst",
    "tests/fixtures/cases.json",
    "proposals/persona_pack/README.md",
])
def test_pytest_collected_code_and_config_are_protected(workspace, relative):
    ws, _home = workspace
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file(relative, PAYLOAD, mode="create")
    assert not (ws / relative).exists()


def test_pytest_config_names_cover_every_name_pytest_searches():
    findpaths = pytest.importorskip("_pytest.config.findpaths")
    searched = list(findpaths.CONFIG_LOADERS_BY_NAME)
    assert set(searched) <= set(file_ops.PYTEST_CONFIG_FILES)
    for name in searched:
        assert name == name.lower()


def test_files_under_the_collected_trees_are_read_protected(workspace):
    ws, _home = workspace
    target = ws / "tests" / "fixtures" / "cases.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError):
        file_ops.read_file("tests/fixtures/cases.json")


def test_pytest_config_inside_a_nested_project_stays_writable(workspace):
    out = file_ops.write_file("project/pytest.ini", "[pytest]\n")
    assert out["bytes"] > 0


@pytest.mark.parametrize("relative", [
    "newpkg/__init__.py",
    "newpkg/__INIT__.py",
    "newpkg/__Init__.PY",
    "newpkg/__init__.pyw",
    "yaml.pyw",
    "tests/sub/test_x.pyw",
    "__pycache__/server.cpython-312.pyc",
    "json.pyd",
    "json.so",
    "scripts/__pycache__/selfmod_oracle.cpython-312.pyc",
])
def test_root_import_path_modules_are_protected(workspace, relative):
    ws, _home = workspace
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file(relative, PAYLOAD, mode="create")
    assert not (ws / relative).exists()


def test_modules_of_an_existing_top_level_package_are_protected(workspace):
    ws, _home = workspace
    package = ws / "toolkit"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for relative in ("toolkit/helpers.py", "toolkit/sub/deep.py"):
        with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
            file_ops.write_file(relative, PAYLOAD, mode="create")
    out = file_ops.write_file("toolkit/README.md", "# notes\n")
    assert out["bytes"] > 0


def test_package_marked_only_by_a_pyw_init_is_protected(workspace):
    ws, _home = workspace
    package = ws / "pkgw"
    package.mkdir()
    (package / "__init__.pyw").write_text("", encoding="utf-8")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file("pkgw/helpers.py", PAYLOAD, mode="create")
    assert not (package / "helpers.py").exists()


def test_module_suffixes_cover_every_import_loader_suffix():
    import importlib.machinery

    for suffix in importlib.machinery.all_suffixes():
        assert "." + suffix.rsplit(".", 1)[-1].casefold() in file_ops.PYTHON_MODULE_SUFFIXES
    assert {".py", ".pyw", ".pyc", ".pyd", ".so"} <= file_ops.PYTHON_MODULE_SUFFIXES


def _zip_members(path: Path, *names: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, PAYLOAD)


@pytest.mark.parametrize(("destination", "member"), [
    ("newpkg", "__init__.py"),
    ("yaml", "__INIT__.py"),
    ("tests/sub", "conftest.py"),
    ("tests/sub", "test_planted.py"),
    ("tests/sub", "pytest.ini"),
    ("tests/sub", "test_planted.txt"),
])
def test_archive_extract_checks_every_member_path(workspace, destination, member):
    ws, _home = workspace
    (ws / "tests").mkdir()
    _zip_members(ws / "drop.zip", member, "notes.txt")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        archive_tools.extract_archive("drop.zip", destination)
    assert not (ws / destination).exists()
    assert not list(ws.glob("**/.sonder-archive-*"))


def test_archive_extract_of_protected_members_with_developer_token(workspace):
    ws, _home = workspace
    _zip_members(ws / "drop.zip", "__init__.py")
    out = archive_tools.extract_archive("drop.zip", "newpkg", developer_authorized=True)
    assert out["ok"] is True
    assert (ws / "newpkg" / "__init__.py").read_text(encoding="utf-8") == PAYLOAD


def test_archive_extract_of_a_plain_project_stays_allowed(workspace):
    ws, _home = workspace
    _zip_members(ws / "drop.zip", "app.py", "src/lib.py", "README.md")
    out = archive_tools.extract_archive("drop.zip", "project")
    assert out["ok"] is True
    assert (ws / "project" / "src" / "lib.py").is_file()


@pytest.mark.parametrize("relative", [
    "evil-1.0.dist-info/METADATA",
    "evil-1.0.dist-info/entry_points.txt",
    "evil.egg-info/entry_points.txt",
    "evil.egg-info/PKG-INFO",
    "Evil-1.0.DIST-INFO/entry_points.txt",
    "evil.Egg-Info/entry_points.txt",
])
def test_distribution_metadata_beside_the_root_is_protected(workspace, relative):
    # importlib.metadata finds *.dist-info / *.egg-info on sys.path (the root
    # under ``python -m pytest``) and pytest autoloads their [pytest11] entry
    # points, which can name a writable namespace-portion module.
    ws, _home = workspace
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file(relative, "[pytest11]\nevil = proj.evil\n", mode="create")
    assert not (ws / relative).exists()


@pytest.mark.parametrize("name", ["evil-1.0.dist-info", "evil.egg-info"])
def test_distribution_metadata_directory_cannot_be_created_or_moved_into(workspace, name):
    ws, _home = workspace
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.make_directory(name)
    assert not (ws / name).exists()
    (ws / name).mkdir()
    (ws / "notes.txt").write_text("[pytest11]\nevil = proj.evil\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.move_file("notes.txt", f"{name}/entry_points.txt")
    assert not (ws / name / "entry_points.txt").exists()


def test_distribution_metadata_inside_a_nested_project_stays_writable(workspace):
    out = file_ops.write_file("project/evil-1.0.dist-info/entry_points.txt", "[pytest11]\n")
    assert out["bytes"] > 0
    out = file_ops.write_file("project/src/evil.egg-info/PKG-INFO", "Name: evil\n")
    assert out["bytes"] > 0


@pytest.mark.parametrize(("destination", "member"), [
    ("evil-1.0.dist-info", "entry_points.txt"),
    ("evil-1.0.dist-info", "METADATA"),
    ("evil.egg-info", "entry_points.txt"),
    ("evil.egg-info", "PKG-INFO"),
])
def test_archive_extract_refuses_distribution_metadata_beside_the_root(workspace, destination, member):
    ws, _home = workspace
    _zip_members(ws / "drop.zip", member)
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        archive_tools.extract_archive("drop.zip", destination)
    assert not (ws / destination).exists()
    assert not list(ws.glob("**/.sonder-archive-*"))


def test_nested_project_without_a_package_marker_stays_writable(workspace):
    out = file_ops.write_file("project/src/app.py", "value = 1\n")
    assert out["bytes"] > 0
    out = file_ops.write_file("project/main.py", "value = 2\n")
    assert out["bytes"] > 0


@pytest.fixture()
def namespace_site(tmp_path, monkeypatch):
    """A site directory holding one installed PEP 420 namespace portion, ``nspkg``.

    It replaces the purelib/platlib/user-site list, so these tests do not
    depend on what the running interpreter has installed.
    """
    site_dir = tmp_path / "fake_site"
    (site_dir / "nspkg" / "sub").mkdir(parents=True)
    (site_dir / "nspkg" / "sub" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(file_ops, "_namespace_site_dirs", lambda: (site_dir.resolve(),))
    monkeypatch.delenv("PYTHONCASEOK", raising=False)
    return site_dir


@pytest.mark.parametrize("relative", [
    "lib/x.py",
    "lib/util.py",
    "Lib/x.py",
    "doc/conf.py",
    "include/x.py",
    "include/gen.py",
    "libs/x.py",
    "tcl/x.py",
    "dlls/x.py",
    "DLLs/x.py",
    "share/x.py",
])
def test_ordinary_nested_project_files_stay_readable_and_writable(workspace, monkeypatch, relative):
    # The interpreter layout under sys.prefix/sys.base_prefix (Lib, DLLs,
    # include, libs, tcl, Doc, Scripts) and pywin32's .pth-added win32\lib,
    # libs, include are not where a distribution installs a namespace portion,
    # and FileFinder matches names case-sensitively (``lib`` is not ``Lib``).
    monkeypatch.delenv("PYTHONCASEOK", raising=False)
    ws, _home = workspace
    out = file_ops.write_file(relative, "x = 1\n", mode="create")
    assert out["bytes"] > 0
    assert "x = 1" in file_ops.read_file(relative)["text"]
    file_ops.edit_file(relative, "x = 1", "x = 2")
    assert (ws / relative).read_text(encoding="utf-8") == "x = 2\n"


@pytest.mark.parametrize("relative", [
    "nspkg/sub/__init__.py",
    "nspkg/sub.py",
    "nspkg/mod.py",
    "nspkg/sub/mod.py",
    "nspkg/__pycache__/sub.cpython-312.pyc",
    "nspkg/sub/__pycache__/__init__.cpython-312.pyc",
    "nspkg/sub/span.pyw",
    "nspkg/sub/ext.pyd",
])
def test_installed_namespace_package_modules_are_protected(workspace, namespace_site, relative):
    # A root portion under <root>/nspkg/ comes first on sys.path under
    # ``python -m pytest`` and adds to or shadows the installed nspkg.
    ws, _home = workspace
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file(relative, PAYLOAD, mode="create")
    assert not (ws / relative).exists()


def test_installed_opentelemetry_namespace_package_is_protected(workspace):
    # The real case from the review: the runtime venv's opentelemetry is a PEP
    # 420 namespace package that ``import mcp`` loads during collection.
    otel = pytest.importorskip("opentelemetry")
    if getattr(otel, "__file__", None) is not None:
        pytest.skip("opentelemetry is a regular package in this interpreter")
    sites = {os.path.normcase(str(place)) for place in file_ops._namespace_site_dirs()}
    portions = [Path(entry).resolve() for entry in otel.__path__]
    if not any(os.path.normcase(str(portion.parent)) in sites for portion in portions):
        pytest.skip("opentelemetry is not installed in a site-packages directory")
    ws, _home = workspace
    for relative in ("opentelemetry/trace/__init__.py", "opentelemetry/trace.py"):
        with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
            file_ops.write_file(relative, PAYLOAD, mode="create")
        assert not (ws / relative).exists()


def test_namespace_portion_names_match_case_sensitively(workspace, namespace_site):
    # FileFinder compares the directory listing exactly: a new ``NSPKG``
    # directory is never a portion of ``nspkg``.
    ws, _home = workspace
    for relative in ("NSPKG/x.py", "NSPKG/sub/mod.py"):
        out = file_ops.write_file(relative, "x = 1\n", mode="create")
        assert out["bytes"] > 0
    assert (ws / "NSPKG" / "sub" / "mod.py").is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive directory aliasing is Windows-specific")
def test_installed_namespace_package_mixed_case_is_protected_on_windows(workspace, namespace_site):
    # The directory already exists as ``nspkg``; another spelling of it writes
    # into that same directory, which FileFinder does match.
    ws, _home = workspace
    (ws / "nspkg").mkdir()
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file("NSPKG/trace.py", PAYLOAD, mode="create")
    assert not (ws / "nspkg" / "trace.py").exists()


@pytest.mark.skipif(
    not sys.platform.startswith(("win", "cygwin", "darwin")),
    reason="PYTHONCASEOK only relaxes import case matching on Windows and macOS",
)
def test_pythoncaseok_relaxes_namespace_portion_matching(workspace, namespace_site, monkeypatch):
    ws, _home = workspace
    monkeypatch.setenv("PYTHONCASEOK", "1")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file("NSPKG/x.py", PAYLOAD, mode="create")
    assert not (ws / "NSPKG" / "x.py").exists()


def test_site_directories_that_are_not_namespace_portions_do_not_count(workspace, namespace_site):
    ws, _home = workspace
    regular = namespace_site / "regular"
    regular.mkdir()
    (regular / "__init__.py").write_text("", encoding="utf-8")
    (regular / "m.py").write_text("", encoding="utf-8")
    (namespace_site / "datadir").mkdir()
    (namespace_site / "datadir" / "readme.txt").write_text("notes\n", encoding="utf-8")
    (namespace_site / "cacheonly" / "__pycache__").mkdir(parents=True)
    (namespace_site / "cacheonly" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"")
    (namespace_site / "empty").mkdir()
    (namespace_site / "shadowed" / "sub").mkdir(parents=True)
    (namespace_site / "shadowed" / "sub" / "m.py").write_text("", encoding="utf-8")
    (namespace_site / "shadowed.py").write_text("", encoding="utf-8")
    for name in ("regular", "datadir", "cacheonly", "empty", "shadowed"):
        out = file_ops.write_file(f"{name}/x.py", "x = 1\n", mode="create")
        assert out["bytes"] > 0
    assert (ws / "shadowed" / "x.py").is_file()


def test_pth_added_interior_directories_do_not_count(workspace, namespace_site, monkeypatch):
    # pywin32's .pth puts site-packages\win32 and win32\lib on sys.path; the
    # children of such a directory (lib, libs, include, test, ...) are not
    # namespace portions a distribution installs. The directory itself sits
    # directly in the site dir, so it is one.
    ws, _home = workspace
    interior = namespace_site / "win32like"
    for child in ("lib", "libs", "include", "test"):
        (interior / child).mkdir(parents=True)
        (interior / child / "m.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [*sys.path, str(interior), str(interior / "lib")])
    for child in ("lib", "libs", "include", "test"):
        out = file_ops.write_file(f"{child}/x.py", "x = 1\n", mode="create")
        assert out["bytes"] > 0
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file("win32like/x.py", PAYLOAD, mode="create")
    assert not (ws / "win32like" / "x.py").exists()


def test_other_sys_path_entries_and_interpreter_dirs_do_not_count(workspace, namespace_site, tmp_path, monkeypatch):
    ws, _home = workspace
    path_entry = tmp_path / "ext_path"
    (path_entry / "vendor_ns" / "sub").mkdir(parents=True)
    (path_entry / "vendor_ns" / "sub" / "m.py").write_text("", encoding="utf-8")
    lib_dir = tmp_path / "ext_lib"
    (lib_dir / "lib_ns" / "sub").mkdir(parents=True)
    (lib_dir / "lib_ns" / "sub" / "m.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(path_entry), *sys.path])
    monkeypatch.setattr(file_ops, "_interpreter_library_dirs", lambda: (lib_dir.resolve(),))
    for relative in ("vendor_ns/m.py", "lib_ns/m.py"):
        out = file_ops.write_file(relative, "x = 1\n", mode="create")
        assert out["bytes"] > 0
    assert (ws / "lib_ns" / "m.py").is_file()


def test_newly_installed_namespace_portion_is_seen(workspace, namespace_site, monkeypatch):
    # The cached portion set is revalidated against the site directories'
    # mtimes; drop the recheck interval so the test does not wait for it.
    monkeypatch.setattr(file_ops, "_NAMESPACE_RECHECK_SECONDS", 0.0)
    ws, _home = workspace
    out = file_ops.write_file("later/x.py", "x = 1\n", mode="create")
    assert out["bytes"] > 0
    (namespace_site / "later" / "sub").mkdir(parents=True)
    (namespace_site / "later" / "sub" / "m.py").write_text("", encoding="utf-8")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file("later/y.py", PAYLOAD, mode="create")
    assert not (ws / "later" / "y.py").exists()


def test_namespace_site_dirs_are_site_packages_only():
    import sysconfig

    def key(raw):
        return os.path.normcase(str(Path(raw).resolve()))

    found = {os.path.normcase(str(place)) for place in file_ops._namespace_site_dirs()}
    paths = sysconfig.get_paths()
    assert key(paths["purelib"]) in found
    assert key(paths["platlib"]) in found
    for excluded in (sys.prefix, sys.base_prefix, paths["stdlib"], paths["platstdlib"], paths["include"]):
        assert key(excluded) not in found


def test_installed_namespace_package_move_read_and_non_module_boundary(workspace, namespace_site):
    ws, _home = workspace
    out = file_ops.write_file("project/trace.py", PAYLOAD, mode="create")
    assert out["bytes"] > 0
    (ws / "nspkg" / "trace").mkdir(parents=True)
    for destination in ("nspkg/trace.py", "nspkg/trace/__init__.py"):
        with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
            file_ops.move_file("project/trace.py", destination)
        assert not (ws / destination).exists()
    target = ws / "nspkg" / "trace.py"
    target.write_text(PAYLOAD, encoding="utf-8")
    with pytest.raises(PermissionError):
        file_ops.read_file("nspkg/trace.py")
    notes = file_ops.write_file("nspkg/README.md", "# notes\n")
    assert notes["bytes"] > 0
    dev = file_ops.write_file(
        "nspkg/trace.py", "ok = True\n", mode="overwrite", developer_authorized=True,
    )
    assert dev["bytes"] > 0


@pytest.mark.parametrize(("destination", "member"), [
    ("nspkg", "trace/__init__.py"),
    ("nspkg", "trace.py"),
    ("nspkg", "__pycache__/trace.cpython-312.pyc"),
    ("nspkg/trace", "__init__.py"),
])
def test_archive_extract_refuses_installed_namespace_package_modules(workspace, namespace_site, destination, member):
    ws, _home = workspace
    if "/" in destination:
        (ws / destination.split("/", 1)[0]).mkdir(parents=True, exist_ok=True)
    _zip_members(ws / "drop.zip", member, "notes.txt")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        archive_tools.extract_archive("drop.zip", destination)
    assert not (ws / destination).exists()
    assert not list(ws.glob("**/.sonder-archive-*"))


@pytest.mark.parametrize("relative", [
    "venv/Lib/site-packages/evil.pth",
    "venv/Lib/site-packages/sitecustomize.py",
    "venv/lib/python3.12/site-packages/requests/__init__.py",
    ".venv/pyvenv.cfg",
])
def test_install_root_venv_is_protected(workspace, relative):
    ws, _home = workspace
    with pytest.raises(PermissionError, match="interpreter environment"):
        file_ops.write_file(relative, "import os\n", mode="create")
    assert not (ws / relative).exists()


def test_interpreter_library_directories_are_protected(workspace, monkeypatch):
    ws, _home = workspace
    library = ws / "pylib" / "site-packages"
    library.mkdir(parents=True)
    monkeypatch.setattr(file_ops, "_interpreter_library_dirs", lambda: (library.resolve(),))
    for name in ("evil.pth", "sitecustomize.py", "usercustomize.py"):
        with pytest.raises(PermissionError, match="interpreter environment"):
            file_ops.write_file(f"pylib/site-packages/{name}", "import os\n", mode="create")
    out = file_ops.write_file("pylib/notes.md", "# notes\n")
    assert out["bytes"] > 0


def test_interpreter_library_directories_include_the_base_installation():
    file_ops._interpreter_library_dirs.cache_clear()
    try:
        places = file_ops._interpreter_library_dirs()
    finally:
        file_ops._interpreter_library_dirs.cache_clear()
    assert file_ops._resolve_best_effort(Path(sys.base_prefix)) in places


def test_library_directory_holding_the_root_does_not_lock_the_workspace(workspace, monkeypatch):
    ws, _home = workspace
    monkeypatch.setattr(file_ops, "_interpreter_library_dirs", lambda: (ws.parent.resolve(),))
    out = file_ops.write_file("notes/readme.md", "# notes\n")
    assert out["bytes"] > 0


def test_interpreter_environment_developer_token_still_writes(workspace):
    out = file_ops.write_file(
        "venv/Lib/site-packages/fix.pth", "\n", developer_authorized=True,
    )
    assert out["bytes"] > 0


def test_reading_interpreter_library_source_stays_allowed(workspace):
    ws, _home = workspace
    target = ws / "venv" / "Lib" / "site-packages" / "lib.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"value = 1\n")
    assert file_ops.read_file("venv/Lib/site-packages/lib.py")["text"] == "value = 1\n"


@pytest.mark.parametrize("relative", [
    "combined_personal.jsonl",
    "data/combined_personal.jsonl",
    "sonder-personal-lora/adapter_model.safetensors",
])
def test_repository_read_resolver_refuses_the_personal_corpus(workspace, relative):
    ws, _home = workspace
    target = ws / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"text": "private"}\n', encoding="utf-8")
    with pytest.raises(PermissionError, match="secret or control state"):
        file_ops.resolve_repository_read_path(relative)


def test_read_file_reads_only_the_capped_prefix(workspace, monkeypatch):
    ws, _home = workspace
    target = ws / "big.txt"
    target.write_bytes(b"a" * (file_ops.MAX_READ_BYTES * 4))

    def whole_file(self):
        raise AssertionError("read_file must not load the whole file")

    monkeypatch.setattr(Path, "read_bytes", whole_file)
    out = file_ops.read_file("big.txt", max_bytes=1000)
    assert out["text"] == "a" * 1000
    assert out["truncated"] is True
    assert out["bytes"] == file_ops.MAX_READ_BYTES * 4


def _database(path: Path, table: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"CREATE TABLE {table} (x)")
        conn.execute(f"INSERT INTO {table} VALUES (1)")
        conn.commit()
    finally:
        conn.close()


def test_sqlite_preview_hash_in_name_opens_that_file(workspace):
    ws, _home = workspace
    _database(ws / "data" / "memory.db", "protected_rows")
    _database(ws / "data" / "memory.db#.db", "own_rows")
    out = file_ops.inspect_data("data/memory.db#.db")
    assert "error" not in out, out
    assert "own_rows" in out["text"]
    assert "protected_rows" not in out["text"]


def test_sqlite_preview_percent_escape_in_name_opens_that_file(workspace):
    ws, _home = workspace
    _database(ws / "data" / "x" / "target.db", "redirected_rows")
    _database(ws / "data" / "x%2Ftarget.db", "own_rows")
    out = file_ops.inspect_data("data/x%2Ftarget.db")
    assert "error" not in out, out
    assert "own_rows" in out["text"]
    assert "redirected_rows" not in out["text"]


def test_sqlite_preview_stops_counting_past_its_deadline(workspace, monkeypatch):
    ws, _home = workspace
    path = ws / "data" / "big.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE rows (x)")
        conn.executemany("INSERT INTO rows VALUES (?)", ((i,) for i in range(5000)))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(file_ops, "INSPECT_SQLITE_SECONDS", -1.0)
    out = file_ops.inspect_data("data/big.db")
    assert out["kind"] == "sqlite", out
    assert out["tables"] == 1
    assert out["text"] == "rows: ? rows"


@pytest.mark.skipif(sys.platform != "win32", reason="extended-length spelling is a Windows path form")
def test_an_extended_length_root_contains_its_plain_spelled_files(workspace, tmp_path, monkeypatch):
    """Candidates lose the extended-length prefix before the containment check; a root written with it must too."""
    outside = tmp_path / "outside"
    outside.mkdir()
    prefix = "\\\\?\\"
    monkeypatch.setenv("SONDER_FILE_ROOTS", prefix + str(outside))
    assert outside.resolve() in file_ops.allowed_roots()
    out = file_ops.write_file(str(outside / "a.txt"), "x\n")
    assert out["bytes"] > 0
    out = file_ops.write_file(prefix + str(outside / "b.txt"), "y\n")
    assert out["bytes"] > 0
    assert (outside / "b.txt").read_text(encoding="utf-8") == "y\n"
    # Control: without the root the same file is outside every allowed root.
    monkeypatch.delenv("SONDER_FILE_ROOTS")
    with pytest.raises(PermissionError, match="outside allowed roots"):
        file_ops.write_file(str(outside / "c.txt"), "z\n")
