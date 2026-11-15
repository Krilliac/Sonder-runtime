"""The confined repo reader: windows, link refusal, sensitive files, caps and the allowlist."""
from __future__ import annotations

import os

import pytest

from sonder_runtime.adapters.security_eval import repo_reader
from sonder_runtime.adapters.security_eval.repo_reader import ConfinedRepo, RepoLimits, RetrievalSettings
from sonder_runtime.domain.security_eval.errors import (
    PathEscapeRefused, RepoRootRefused, SourceRefused, SourceUnavailable)
from sonder_runtime.domain.security_eval.records import LineRange
from tests.support.security_eval_fixtures import build_repo

pytestmark = pytest.mark.unit


def make_dir_link(link, target):
    """A directory junction on Windows (``_winapi.CreateJunction``), a symlink elsewhere."""
    if os.name == "nt":
        try:
            import _winapi
            create = _winapi.CreateJunction
        except (ImportError, AttributeError):
            pytest.skip("_winapi.CreateJunction is unavailable, so a junction cannot be created")
        create(str(target), str(link))
        return
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("os.symlink is not permitted here, so a directory link cannot be created")


@pytest.fixture
def repo_root(tmp_path):
    return build_repo(tmp_path / "repo")


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "stolen.cs").write_text("class Stolen { void ReadBlock() {} }\n", encoding="utf-8")
    (folder / "caller.py").write_text("print('outside')\n", encoding="utf-8")
    return folder


def open_repo(repo_root, tmp_path, **limits):
    return ConfinedRepo(str(repo_root), [str(tmp_path)], RepoLimits(**limits) if limits else RepoLimits(),
                        RetrievalSettings())


READERS = {
    "read_window": lambda repo, rel: repo.read_window(rel, LineRange(1, 1), 0),
    "read_chunks": lambda repo, rel: repo.read_chunks(rel, 400),
    "retrieve": lambda repo, rel: repo.retrieve(rel, LineRange(1, 1)),
}


def assert_refused_by_every_reader(repo, rel, match=None):
    """read_window, read_chunks and retrieve each raise ``SourceRefused`` for ``rel``."""
    for name, read in READERS.items():
        with pytest.raises(SourceRefused, match=match):
            read(repo, rel)
            pytest.fail("%s read %r" % (name, rel))


def test_window_read_with_padding_and_clamping(repo_root, tmp_path):
    repo = open_repo(repo_root, tmp_path)
    window = repo.read_window("src/PacketReader.cs", LineRange(13, 14), 1)
    assert (window.start, window.end) == (12, 15)
    assert window.text.splitlines()[1].strip() == "var buffer = new byte[length];"
    head = repo.read_window("src/PacketReader.cs", LineRange(1, 2), 20)
    assert head.start == 1 and head.total_lines == 25 and head.end == 22


def test_a_range_past_the_end_of_the_file_is_unavailable(repo_root, tmp_path):
    repo = open_repo(repo_root, tmp_path)
    with pytest.raises(SourceUnavailable):
        repo.read_window("src/PacketReader.cs", LineRange(500, 501), 0)
    with pytest.raises(SourceUnavailable):
        repo.read_window("src/missing.cs", LineRange(1, 2), 0)


@pytest.mark.parametrize("rel", ["../outside/stolen.cs", "src/../../outside/stolen.cs", "C:/Windows/win.ini",
                                 "src/PacketReader.cs:stream", "a:b", "\\\\host\\share\\x", ""])
def test_escaping_file_fields_are_refused(repo_root, tmp_path, rel):
    repo = open_repo(repo_root, tmp_path)
    with pytest.raises(PathEscapeRefused):
        repo.read_window(rel, LineRange(1, 1), 0)


def test_an_absolute_path_is_refused(repo_root, tmp_path):
    repo = open_repo(repo_root, tmp_path)
    with pytest.raises(PathEscapeRefused):
        repo.read_window(str(repo_root / "src" / "PacketReader.cs"), LineRange(1, 1), 0)


def test_a_linked_directory_pointing_outside_is_neither_walked_nor_opened(repo_root, tmp_path, outside):
    make_dir_link(repo_root / "linked", outside)
    repo = open_repo(repo_root, tmp_path)
    repo.retrieve("src/PacketReader.cs", LineRange(13, 14))
    assert not any(path.startswith("linked") for path in repo.indexed_paths())
    with pytest.raises(PathEscapeRefused):
        repo.read_window("linked/stolen.cs", LineRange(1, 1), 0)
    bundle = repo.retrieve("src/PacketReader.cs", LineRange(13, 14))
    assert "Stolen" not in bundle.text and "outside" not in bundle.text


def test_a_directory_swapped_for_a_link_after_indexing_is_refused_at_open(repo_root, tmp_path, outside):
    repo = open_repo(repo_root, tmp_path)
    repo.retrieve("src/PacketReader.cs", LineRange(13, 14))
    assert "tools/caller.py" in repo.indexed_paths()
    assert repo.read_window("tools/caller.py", LineRange(1, 1), 0).text.startswith("from reader")
    os.rename(repo_root / "tools", repo_root / "tools_old")
    make_dir_link(repo_root / "tools", outside)
    with pytest.raises(PathEscapeRefused):
        repo.read_window("tools/caller.py", LineRange(1, 1), 0)


def test_sensitive_secret_env_and_binary_files_are_skipped_and_refused(repo_root, tmp_path):
    repo = open_repo(repo_root, tmp_path)
    repo.retrieve("src/PacketReader.cs", LineRange(13, 14))
    indexed = set(repo.indexed_paths())
    assert {"src/PacketReader.cs", "tools/caller.py", "nested/deep/note.txt"} <= indexed
    assert not indexed & {".env", "src/server.pem", ".git/config", "src/blob.bin"}
    for rel in (".env", "src/server.pem", ".git/config", "src/blob.bin"):
        assert_refused_by_every_reader(repo, rel)


def test_a_file_over_the_per_file_cap_is_skipped_and_unavailable(repo_root, tmp_path):
    repo = open_repo(repo_root, tmp_path, max_file_bytes=300)
    repo.retrieve("tools/caller.py", LineRange(8, 8))
    assert "src/PacketReader.cs" not in repo.indexed_paths()
    with pytest.raises(SourceUnavailable):
        repo.read_window("src/PacketReader.cs", LineRange(1, 1), 0)


def test_file_count_cap_is_reported_as_truncation(repo_root, tmp_path):
    repo = open_repo(repo_root, tmp_path, max_files=2)
    bundle = repo.retrieve("tools/caller.py", LineRange(8, 8))
    assert len(repo.indexed_paths()) == 2
    assert "max_files" in repo.index_report()["truncated"]
    assert "max_files" in bundle.skipped["index_truncated"]


def test_byte_cap_and_timeout_are_reported(repo_root, tmp_path):
    repo = open_repo(repo_root, tmp_path, max_bytes=400)
    repo.retrieve("tools/caller.py", LineRange(8, 8))
    assert "max_bytes" in repo.index_report()["truncated"]
    ticks = iter(range(0, 10_000, 100))
    slow = ConfinedRepo(str(repo_root), [str(tmp_path)], RepoLimits(timeout_seconds=150),
                        RetrievalSettings(), clock=lambda: next(ticks))
    slow.retrieve("tools/caller.py", LineRange(8, 8))
    assert "timeout" in slow.index_report()["truncated"]


def test_root_outside_the_allowlist_is_refused(repo_root, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(RepoRootRefused):
        ConfinedRepo(str(repo_root), [str(elsewhere)], RepoLimits(), RetrievalSettings())


def test_an_empty_allowlist_refuses_every_root(repo_root):
    with pytest.raises(RepoRootRefused):
        ConfinedRepo(str(repo_root), [], RepoLimits(), RetrievalSettings())


def test_a_sibling_with_a_common_prefix_is_not_inside_the_allowlist(tmp_path):
    allowed = tmp_path / "work"
    allowed.mkdir()
    sibling = build_repo(tmp_path / "work-evil")
    with pytest.raises(RepoRootRefused):
        ConfinedRepo(str(sibling), [str(allowed)], RepoLimits(), RetrievalSettings())


@pytest.mark.parametrize("root", ["relative/repo", "", "missing-dir-for-sure"])
def test_relative_empty_or_missing_roots_are_refused(tmp_path, root):
    with pytest.raises(RepoRootRefused):
        ConfinedRepo(root, [str(tmp_path)], RepoLimits(), RetrievalSettings())


def test_a_root_that_is_a_link_is_refused(repo_root, tmp_path):
    link = tmp_path / "linkroot"
    make_dir_link(link, repo_root)
    with pytest.raises(RepoRootRefused):
        ConfinedRepo(str(link), [str(tmp_path)], RepoLimits(), RetrievalSettings())


def test_a_file_as_root_is_refused(repo_root, tmp_path):
    with pytest.raises(RepoRootRefused):
        ConfinedRepo(str(repo_root / "tools" / "caller.py"), [str(tmp_path)], RepoLimits(), RetrievalSettings())


def test_read_chunks_split_on_line_boundaries(repo_root, tmp_path):
    repo = open_repo(repo_root, tmp_path)
    chunks = repo.read_chunks("src/PacketReader.cs", 400)
    assert len(chunks) >= 2
    assert chunks[0].start == 1 and chunks[-1].end == chunks[-1].total_lines
    for earlier, later in zip(chunks, chunks[1:], strict=False):
        assert later.start == earlier.end + 1
    assert all(len(chunk.text) <= 400 for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks).count("ReadBlock") == 1


@pytest.mark.parametrize("rel", [
    "deploy/prod.env", "config/secrets.yaml", ".docker/config.json",
    ".env-prod", "config/.env_local", "deploy/.envs", "deploy/.ENV-Staging",
])
def test_env_suffix_credential_named_and_docker_config_files_are_skipped_and_refused(repo_root, tmp_path, rel):
    target = repo_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("KEY=hunter2", encoding="utf-8")
    repo = open_repo(repo_root, tmp_path)
    repo.retrieve("src/PacketReader.cs", LineRange(13, 14))
    assert rel not in set(repo.indexed_paths())
    assert_refused_by_every_reader(repo, rel)


@pytest.mark.parametrize("rel", [
    ".env", "deploy/prod.env", "config/secrets.yaml", ".docker/config.json", ".env-prod",
    "src/server.pem", ".ssh/id_ed25519", "home/.aws/credentials", ".git/HEAD",
])
def test_a_secret_path_is_refused_before_anything_is_opened(repo_root, tmp_path, rel):
    """The name check runs first: a missing secret path is refused, not reported missing.

    The opened-handle check would also refuse an existing secret file, so only a path that does not
    exist shows that each reader runs the name check itself before opening anything.
    """
    target = repo_root / rel
    if target.exists():
        target.unlink()
    repo = open_repo(repo_root, tmp_path)
    assert_refused_by_every_reader(repo, rel, match="sensitive or secret path and is never read")


@pytest.mark.parametrize("part, shaped", [
    ("ENV~1", True), ("CREDEN~1.JSO", True), ("SSH~1", True), ("GIT-CR~1", True), ("env~12", True),
    ("A~BC~1.TXT", True), ("backup~1.txt", True), ("report~2024.md", False), ("page~1.html", False),
    ("PacketReader.cs", False), ("x~y.txt", False), ("~", False), ("ABCDEFG~1", False),
])
def test_the_short_name_shape(part, shaped):
    assert repo_reader._looks_like_short_name(part) is shaped


@pytest.mark.parametrize("rel", ["ABCDEF~1.TXT", "SSH~1/data.txt", "src/GIT~1/config", "env~1"])
def test_a_component_shaped_like_a_short_name_is_refused_before_anything_is_opened(repo_root, tmp_path, rel):
    repo = open_repo(repo_root, tmp_path)
    assert_refused_by_every_reader(repo, rel, match="8.3 short name")


def test_a_long_name_with_a_tilde_that_is_not_short_name_shaped_is_read(repo_root, tmp_path):
    (repo_root / "src" / "report~2024.md").write_text("plain notes\n", encoding="utf-8")
    repo = open_repo(repo_root, tmp_path)
    assert repo.read_window("src/report~2024.md", LineRange(1, 1), 0).text == "plain notes"
    assert "src/report~2024.md" in repo.indexed_paths()


def _short_component(path):
    """The 8.3 short name of the last component of ``path`` (Windows ``GetShortPathNameW``)."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetShortPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(1024)
    if not function(str(path), buffer, len(buffer)):
        raise OSError(ctypes.get_last_error(), "GetShortPathNameW failed for %s" % path)
    return os.path.basename(buffer.value)


# Secret files and files under skipped directories, each reached by its 8.3 alias.
SHORT_NAME_TARGETS = [
    ".env", ".env-production", "credentials.json", "secrets.yaml", ".netrc", ".git-credentials",
    ".pgpass", "token.json", ".envrc", ".ssh/data.txt", ".git/data.txt", ".aws/data.txt",
]


@pytest.fixture
def short_aliases(repo_root):
    """Create every ``SHORT_NAME_TARGETS`` file and map it to its short-name alias relative path."""
    if os.name != "nt":
        pytest.skip("8.3 short names exist only on Windows volumes")
    aliases = {}
    for rel in SHORT_NAME_TARGETS:
        target = repo_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("SECRET=hunter2-%s\n" % rel, encoding="utf-8")
        parts = rel.split("/")
        short_parts = [_short_component(repo_root.joinpath(*parts[:index + 1])) for index in range(len(parts))]
        if short_parts[0].casefold() == parts[0].casefold():
            pytest.skip("the volume holding %s generates no 8.3 short names (%r has none); run with a "
                        "C:-backed --basetemp" % (repo_root, parts[0]))
        aliases[rel] = "/".join(short_parts)
    return aliases


def test_short_name_aliases_of_secret_files_and_skipped_directories_are_refused(repo_root, tmp_path,
                                                                                short_aliases):
    repo = open_repo(repo_root, tmp_path)
    for alias in short_aliases.values():
        assert_refused_by_every_reader(repo, alias)
    indexed = set(repo.indexed_paths())
    assert not indexed & (set(short_aliases) | set(short_aliases.values()))
    assert "hunter2" not in repo.retrieve("src/PacketReader.cs", LineRange(13, 14)).text


def test_the_opened_handle_check_alone_refuses_short_name_aliases(repo_root, tmp_path, short_aliases,
                                                                  monkeypatch):
    """With the name-shape filter off, the handle-path check is what refuses every alias."""
    monkeypatch.setattr(repo_reader, "_looks_like_short_name", lambda part: False)
    repo = open_repo(repo_root, tmp_path)
    for alias in short_aliases.values():
        assert_refused_by_every_reader(repo, alias, match="opens a sensitive or secret path")


def test_the_read_is_refused_when_the_opened_handle_path_cannot_be_obtained(repo_root, tmp_path,
                                                                           monkeypatch):
    if os.name != "nt":
        pytest.skip("POSIX falls back to samestat when the descriptor cannot be named")

    def unavailable(descriptor):
        raise OSError(5, "access denied")

    monkeypatch.setattr(repo_reader, "_opened_handle_path", unavailable)
    repo = open_repo(repo_root, tmp_path)
    assert_refused_by_every_reader(repo, "src/PacketReader.cs", match="could not be confirmed")
    monkeypatch.setattr(repo_reader, "_opened_handle_path", lambda descriptor: None)
    assert_refused_by_every_reader(repo, "src/PacketReader.cs", match="could not be confirmed")
