"""Containment must not get WEAKER when ``Path.resolve()`` degrades.

``file_ops`` guards three ``resolve()`` calls with ``except OSError`` and falls
back to ``Path.absolute()`` (``allowed_roots``, ``_resolve_best_effort``,
``resolve_path``). ``absolute()`` does not normalize ``..``, and ``_is_inside``
compares raw strings with ``os.path.commonpath``, which treats ``..`` as an
ordinary path component. Measured on Python 3.12.10 / Windows against the real
workspace root::

    resolve()  -> C:\\Users\\<user>\\Windows\\System32\\config\\SAM   is_inside=False
    absolute() -> <root>\\..\\..\\..\\Windows\\System32\\config\\SAM is_inside=True

i.e. the error path ACCEPTED an escaping path the success path REFUSED. A
containment guard that becomes permissive on its own error branch is wrong
regardless of how the branch is entered.

Honest scope note on reachability: the mechanism above is reproduced directly,
but the TRIGGER is not demonstrated. On this host ``Path.resolve()`` could not
be made to raise ``OSError``. What was checked and did NOT raise: over-long
paths, >32k paths (those raise ``ValueError``, which this handler does not
catch), dead UNC, device paths, reserved names, invalid characters, 400 ``..``
segments, unmounted drives, drive-relative forms, locked files (pagefile,
SAM, hiberfil) -- every underlying ``winerror`` landed inside the 15-code
allow-list ``ntpath._getfinalpathname_nonstrict`` swallows, and file-descriptor
exhaustion (8189 handles) did not reach ``_getfinalpathname``, which bypasses
the CRT. Two escape routes exist in CPython but neither is reachable here:
``ntpath.realpath`` calls ``os.getcwd()`` unconditionally (Lib/ntpath.py:719,
even for an absolute path), but Windows refuses to delete a live process's cwd
(WinError 32), and POSIX ``realpath`` skips ``getcwd`` for absolute paths (both
verified); and ``_getfinalpathname_nonstrict`` re-raises any ``winerror``
outside its allow-list (Lib/ntpath.py:684), but no such code could be produced.

So these tests enter the branch by forcing the exception rather than by
provoking it. That pins the guard's behaviour on a branch the code already
commits to handling -- the fallback is written, so it must be safe.
"""
from pathlib import Path

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops


def _raise_oserror(self, *args, **kwargs):
    """Stand-in for ``Path.resolve`` on the branch under test."""
    raise OSError(5, "forced resolve failure")


@pytest.fixture()
def isolated_roots(monkeypatch, tmp_path):
    """Workspace root at ``tmp_path/root`` with no operator-configured roots."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(tmp_path / "no-such-roots"))
    return root


def test_is_inside_rejects_a_dotdot_escape_it_would_treat_as_literal():
    """``_is_inside`` must not count ``..`` as an ordinary path component.

    ``os.path.commonpath`` is purely lexical, so ``commonpath([root + '/../x',
    root]) == root`` -- containment answered YES for a path that escapes. This
    is the primitive every containment decision funnels through, so it must be
    correct for un-normalized input regardless of which caller produced it.
    """
    root = Path("C:/duet/root") if Path("C:/").drive else Path("/duet/root")
    escaping = root / ".." / ".." / "etc" / "passwd"

    assert file_ops._is_inside(root / "sub" / "file.txt", root) is True
    assert file_ops._is_inside(escaping, root) is False


def test_resolve_path_refuses_escape_when_resolve_degrades(
    monkeypatch, isolated_roots,
):
    """``resolve_path``'s ``except OSError`` branch must stay contained.

    Before the fix this returned the escaping path instead of raising: the
    ``absolute()`` fallback kept the literal ``..`` segments and ``_is_inside``
    read them as being under the root, so the guarded file tools would have
    handed a caller a path outside every allowed root -- the exact inversion
    the module's success path refuses.
    """
    root = isolated_roots
    escaping = root / ".." / ".." / "outside" / "secret.txt"
    monkeypatch.setattr(Path, "resolve", _raise_oserror)

    with pytest.raises(PermissionError, match="outside allowed roots"):
        file_ops.resolve_path(str(escaping))


def test_resolve_path_still_accepts_a_contained_path_when_resolve_degrades(
    monkeypatch, isolated_roots,
):
    """The hardening must not make the fallback refuse legitimate paths.

    Normalizing is only allowed to move a path OUT of a root, never into one;
    a path genuinely under the root must still resolve on the degraded branch.
    """
    root = isolated_roots
    inside = root / "sub" / ".." / "sub" / "notes.txt"
    monkeypatch.setattr(Path, "resolve", _raise_oserror)

    assert file_ops.resolve_path(str(inside)) == root / "sub" / "notes.txt"


def test_resolve_best_effort_normalizes_on_the_degraded_branch(monkeypatch, tmp_path):
    """``_resolve_best_effort`` feeds the control-plane classifiers.

    It is what ``_is_sensitive_control_path`` / ``_is_protected_mutation_path``
    canonicalize with, so an un-normalized return leaks ``..`` into every
    downstream comparison (``path.parent == root``, set membership in
    ``_control_plane_paths()``), which silently fails to match and lets a
    protected path through the mutation guard.
    """
    root = tmp_path.resolve()
    base = root / "a" / "b"
    base.mkdir(parents=True)
    target = base / ".." / ".." / "c.txt"
    monkeypatch.setattr(Path, "resolve", _raise_oserror)

    assert file_ops._resolve_best_effort(target) == root / "c.txt"


def test_allowed_roots_normalizes_on_the_degraded_branch(monkeypatch, tmp_path):
    """A root that degrades must not be published with literal ``..``.

    ``allowed_roots`` returns the comparison targets for every containment
    check; an un-normalized root widens containment for every path tested
    against it, not just for the one call that degraded.
    """
    root = (tmp_path / "root").resolve()
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.delenv("SONDER_FILE_ROOTS_FILE", raising=False)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root / "sub" / ".." / "sub"))
    monkeypatch.setattr(Path, "resolve", _raise_oserror)

    assert (root / "sub") in file_ops.allowed_roots()
    assert all(".." not in r.parts for r in file_ops.allowed_roots())
