"""Private, locked, fsync-backed canonical replay state before admission.

There is no reset/migration API. An initialized directory missing its record
fails closed, including after restart. Wholesale rollback of the directory by
its owner requires an independent monotonic anchor to detect across restarts;
this local record does not provide that anchor.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat

from ...application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from ...domain.inference_membership import MembershipHighWater, MembershipSnapshot, _aware
from ..persistence.postgres_binding import _private_file


class MembershipStateError(RuntimeError):
    """Private state details never cross the membership boundary."""


def _encode(water):
    return json.dumps(dict(cluster=water.cluster_id, issuer=water.issuer_id,
                           generation=water.generation, digest=water.digest),
                      sort_keys=True, separators=(",", ":")).encode("ascii")


def _identity(anchor, name, stream):
    anchor.validate()
    _private_file(stream)
    metadata = (anchor.path / name).lstat() if os.name == "nt" else os.stat(name, dir_fd=anchor.fd, follow_symlinks=False)
    if (not stat.S_ISREG(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & 0x400
            or not os.path.samestat(metadata, os.fstat(stream.fileno()))):
        raise ValueError


def _open_lock(anchor):
    if os.name != "nt":
        return anchor.open_read("state.lock")
    # Prevent lock-file replacement while the OS byte lock is held.
    import ctypes
    import msvcrt
    create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    handle = create(str(anchor.path / "state.lock"), 0x80000000, 3, None, 3, 0x00200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError("membership lock unavailable")
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close.argtypes, close.restype = [ctypes.c_void_p], ctypes.c_int
        close(handle)
        raise
    return os.fdopen(fd, "rb")


def _lock(stream, *, release=False):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


class MembershipHighWaterStore:
    def __init__(self, path, *, cluster_id, issuer_id, clock):
        MembershipHighWater(cluster_id, issuer_id, 1, "0" * 64)
        _aware(clock(), "membership clock")
        self._path = Path(os.path.abspath(path))
        if self._path.name != "high-water.json":
            raise ValueError("invalid membership state location")
        self._cluster, self._issuer, self._clock = cluster_id, issuer_id, clock
        self._root_identity = None

    @contextmanager
    def _session(self, *, create=False):
        root = self._path.parent
        if os.path.normcase(os.path.realpath(root.parent)) != os.path.normcase(str(root.parent)):
            raise ValueError
        created = False
        if not os.path.lexists(root):
            if self._root_identity is not None:
                raise ValueError
            if not create:
                yield None
                return
            anchor = PrivateDirectoryAnchor.open_base(root, require_new=True)
            created = True
        else:
            anchor = PrivateDirectoryAnchor(root)
        with anchor:
            identity = root.lstat()
            if self._root_identity is not None and not os.path.samestat(identity, self._root_identity):
                raise ValueError
            self._root_identity = identity
            if created:
                fd, temporary = anchor.create_temporary()
                with os.fdopen(fd, "wb") as stream:
                    stream.write(b"0")
                    stream.flush()
                    os.fsync(stream.fileno())
                anchor.publish(temporary, "state.lock")
                if os.name != "nt":
                    os.fsync(anchor.fd)
                    parent = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try: os.fsync(parent)
                    finally: os.close(parent)
            with _open_lock(anchor) as lock:
                _identity(anchor, "state.lock", lock)
                if os.fstat(lock.fileno()).st_size != 1:
                    raise ValueError
                _lock(lock)
                try:
                    _identity(anchor, "state.lock", lock)
                    yield anchor, lock, created
                    _identity(anchor, "state.lock", lock)
                finally:
                    _lock(lock, release=True)

    def _record(self, anchor, lock):
        _identity(anchor, "state.lock", lock)
        with anchor.open_read(self._path.name) as stream:
            _identity(anchor, self._path.name, stream)
            raw = stream.read(2049)
            _identity(anchor, self._path.name, stream)
        _identity(anchor, "state.lock", lock)
        if len(raw) > 2048:
            raise ValueError
        record = json.loads(raw)
        if type(record) is not dict or set(record) != {"cluster", "issuer", "generation", "digest"}:
            raise ValueError
        water = MembershipHighWater(record["cluster"], record["issuer"], record["generation"], record["digest"])
        if raw != _encode(water) or water.cluster_id != self._cluster or water.issuer_id != self._issuer:
            raise ValueError
        return water

    @staticmethod
    def _commit(publish):
        publish()

    def _replace(self, anchor, lock, water, old):
        fd, temporary = anchor.create_temporary()
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(_encode(water))
                stream.flush()
                os.fsync(stream.fileno())
                _identity(anchor, temporary, stream)
            def publish():
                _identity(anchor, "state.lock", lock)
                if old is None:
                    if anchor.exists(self._path.name): raise ValueError
                elif _encode(self._record(anchor, lock)) != _encode(old):
                    raise ValueError
                with anchor.open_read(temporary) as staged:
                    _identity(anchor, temporary, staged)
                    if staged.read(2049) != _encode(water): raise ValueError
                    _identity(anchor, "state.lock", lock)
                    if os.name == "nt":
                        import ctypes
                        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
                        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
                        move.restype = ctypes.c_int
                        if not move(str(anchor.path / temporary), str(self._path), 1 | 8):
                            raise OSError("membership publication unavailable")
                    else:
                        os.replace(temporary, self._path.name, src_dir_fd=anchor.fd, dst_dir_fd=anchor.fd)
                        os.fsync(anchor.fd)
                    _identity(anchor, self._path.name, staged)
                _identity(anchor, "state.lock", lock)
            self._commit(publish)
        finally:
            anchor.validate()
            if anchor.exists(temporary): anchor.unlink(temporary)

    def read(self):
        try:
            with self._session() as session:
                return None if session is None else self._record(session[0], session[1])
        except Exception:
            raise MembershipStateError("membership replay state unavailable") from None

    def compare_and_advance(self, snapshot):
        try:
            if type(snapshot) is not MembershipSnapshot:
                raise ValueError
            now = _aware(self._clock(), "membership clock")
            if not snapshot.issued_at <= now < snapshot.expires_at:
                raise ValueError
            water = MembershipHighWater(snapshot.cluster_id, snapshot.issuer_id, snapshot.generation, snapshot.digest)
            if water.cluster_id != self._cluster or water.issuer_id != self._issuer:
                raise ValueError
            with self._session(create=True) as (anchor, lock, created):
                old = None if created else self._record(anchor, lock)
                if old is not None:
                    if water.generation < old.generation or (water.generation == old.generation and water.digest != old.digest):
                        raise ValueError
                    if water.generation == old.generation:
                        return old
                self._replace(anchor, lock, water, old)
                return water
        except Exception:
            raise MembershipStateError("membership replay state unavailable") from None
