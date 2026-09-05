"""Single-process coordination for exact owned account databases."""

from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import threading

_LOCK = threading.Lock()
_GUARDS = {}


@contextmanager
def account_admission(connection):
    rows = connection.execute("PRAGMA database_list").fetchall()
    path = next((row[2] for row in rows if row[1] == "main"), "")
    # Legacy disposable in-memory databases cannot share authority with a
    # persistent HTTP control store. Still serialize their own connection.
    if path:
        source = Path(path).resolve()
        meta = source.stat()
        key = (str(source), meta.st_dev, meta.st_ino)
    else:
        key = ("memory", id(connection))
    with _LOCK:
        entry = _GUARDS.get(key)
        if entry is None:
            if len(_GUARDS) >= 128:
                raise PermissionError("account admission capacity unavailable")
            entry = [threading.RLock(), 0]
            _GUARDS[key] = entry
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _LOCK:
            entry[1] -= 1
            if entry[1] == 0:
                _GUARDS.pop(key, None)


def coordinated_account_mutation(function):
    @wraps(function)
    def wrapped(connection, *args, **kwargs):
        with account_admission(connection):
            return function(connection, *args, **kwargs)

    return wrapped


_PASSWORD_SLOTS = threading.BoundedSemaphore(2)
_PASSWORD_LOCK = threading.Lock()
_PASSWORD_RATE = {}


@contextmanager
def password_admission(connection, principal):
    """Bound app-control password work by process and exact account database."""
    import time

    path = next(
        row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"
    )
    key = (str(Path(path).resolve()), principal)
    if not _PASSWORD_SLOTS.acquire(blocking=False):
        from ...application.ports.app_control import CapacityExceeded

        raise CapacityExceeded("password admission full")
    try:
        now = time.monotonic()
        with _PASSWORD_LOCK:
            for prior in tuple(_PASSWORD_RATE):
                if _PASSWORD_RATE[prior][1] + 60 <= now:
                    _PASSWORD_RATE.pop(prior)
            count, start = _PASSWORD_RATE.get(key, (0, now))
            if count >= 8 or key not in _PASSWORD_RATE and len(_PASSWORD_RATE) >= 512:
                from ...application.ports.app_control import CapacityExceeded

                raise CapacityExceeded("password admission full")
            _PASSWORD_RATE[key] = (count + 1, start)
        yield
    finally:
        _PASSWORD_SLOTS.release()
