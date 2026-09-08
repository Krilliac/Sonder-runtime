"""Local OS file-lock owner evidence; never a remote lease or takeover policy."""

import os
import re
from pathlib import Path


def _lock(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class LocalLaneOwner:
    def __init__(self, database_path, owner):
        self.path = Path(database_path).parent / (owner + ".lock")
        self.handle = self.path.open("x+b")
        self.handle.write(b"0")
        self.handle.flush()
        _lock(self.handle)

    def close(self):
        self.handle.close()


def owner_definitely_stopped(database_path, owner):
    """Return true only after acquiring the previously published owner's lock.

    Missing files, invalid identities, I/O errors and held locks are not proof
    of owner death. Kernel locks are released when a local process exits.
    """
    if not re.fullmatch(r"lane-owner-[0-9a-f]{32}", owner):
        return False
    path = Path(database_path).parent / (owner + ".lock")
    try:
        with path.open("r+b") as handle:
            _lock(handle)
            return True
    except OSError:
        return False
