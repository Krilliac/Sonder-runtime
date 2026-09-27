"""Evaluator-held store and candidate channel for the independent oracle (#517).

The comparison rules and the durable receipt live in
``sonder_runtime/application/selfmod/independent_oracle.py``.  This module
owns everything that touches the host:

* **Storage.**  Held-out cases for one ``module.function`` live in
  ``<oracle home>/<module>.<function>.json``.  The oracle home is
  ``SONDER_SELFMOD_ORACLE_HOME`` (absolute) or, when that is unset and no
  typed state home is configured in the process, ``<selfmod state
  root>/oracle`` (:func:`oracle_home`); it is created ``0700`` and owned by
  the evaluator, and every case file is
  written ``0600`` with ``O_NOFOLLOW`` and an atomic replace.  The candidate
  checkout never contains them.
* **Confidentiality.**  On a host where the Linux uid supervisor is selected,
  :func:`confidentiality` requires the case file and its directory to be
  closed to the candidate uid (``require_not_candidate_readable`` and
  ``require_not_candidate_writable``), and :func:`prove_read_denied` then
  runs a probe of evaluator code *as the candidate uid* that must receive
  ``EACCES`` when it opens the case file and lists the oracle home.  The
  Windows low-integrity supervisor does not bound reads, so there the oracle
  is never confidential and never independent.
* **Channel.**  :func:`challenge_command` builds the candidate-side probe.
  It receives the per-run nonce and ``(token, args, kwargs)`` triples in a
  random order, imports the candidate module from the candidate root, calls
  the function once per case and prints a single
  ``SELFMOD ORACLE OUTPUTS`` frame of raw outputs (or raised exception class
  names).  It never receives an expected value.

Operators provision cases with::

    python scripts/selfmod_oracle.py provision --module reflection \\
        --function selected --cases held_cases.json
    python scripts/selfmod_oracle.py inspect --module reflection --function selected

``held_cases.json`` is a list of ``{"args": [...], "kwargs": {...},
"expected": value}`` or ``{"args": [...], "raises": "ValueError"}`` objects;
delete it after provisioning.  ``inspect`` never prints expected values.  A
set whose challenge (for any candidate root up to PATH_MAX) or all-correct
result frame would exceed the channel bound is refused at provisioning.
Run both commands with the same environment as the nightly; set
``SONDER_SELFMOD_ORACLE_HOME`` when the runtime uses a typed ``state.home``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sonder_runtime.application.selfmod.candidate_isolation import LINUX_UID
from sonder_runtime.application.selfmod.independent_oracle import (
    CASE_SET_VERSION,
    ORACLE_FRAME_PREFIX,
    CaseSet,
    HeldCase,
    OracleChallenge,
    OracleError,
    challenge_payload,
)

ORACLE_HOME_ENV = "SONDER_SELFMOD_ORACLE_HOME"
_MAX_CASE_FILE_BYTES = 1024 * 1024
SEAL_PREFIX = "SELFMOD ORACLE SEAL "
_SEAL_DENIED = SEAL_PREFIX + "read=denied list=denied"
_SEAL_TIMEOUT_SECONDS = 60


class OracleUnavailable(RuntimeError):
    """The held case set cannot be used as evaluator truth for this run."""


def oracle_home() -> Path:
    """The evaluator-owned directory holding held-out case sets.

    The operator CLI and the nightly must resolve the same directory, so
    the result depends only on the environment: ``SONDER_SELFMOD_ORACLE_HOME``
    (which must be absolute, so the working directory cannot move it), else
    ``<selfmod state root>/oracle``.  A process that composed a typed state
    home (``paths.configure_home``, e.g. a runtime started with
    ``state.home``) resolves the selfmod state root differently from a bare
    ``python scripts/selfmod_oracle.py``; there the default is refused and
    the explicit variable is required, so the nightly reports the oracle as
    unusable instead of silently finding no cases.
    """
    configured = os.environ.get(ORACLE_HOME_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise OracleUnavailable(f"{ORACLE_HOME_ENV} must be an absolute path")
        return path
    from sonder_runtime.platform import paths

    if paths.configured_home() is not None:
        raise OracleUnavailable(
            f"a typed state home is configured in this process, so the default oracle "
            f"home differs from the one the operator CLI provisions; set {ORACLE_HOME_ENV} "
            f"to an absolute path for both")
    import selfmod

    return selfmod.state_root() / "oracle"


def case_path(module: str, function: str, *, home: Path | None = None) -> Path:
    """The case-set file for ``module.function`` (names validated)."""
    if not module or not all(part.isidentifier() for part in str(module).split(".")) \
            or not str(function).isidentifier():
        raise OracleError("oracle target must be an importable module and function name")
    return Path(home if home is not None else oracle_home()) / f"{module}.{function}.json"


def _ensure_home(home: Path) -> None:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = home.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OracleUnavailable(f"oracle home is not a real directory: {home}")
    if os.name != "nt":
        if info.st_uid != os.geteuid():
            raise OracleUnavailable(f"oracle home is not owned by the evaluator: {home}")
        os.chmod(home, 0o700)


def provision(module: str, function: str, cases: Sequence[object], *,
              home: Path | None = None) -> str:
    """Write the held case set for ``module.function``; return its SHA-256.

    The file is created ``0600`` next to its final name and atomically
    replaces any previous set.  Cases are validated with the same contract
    the grader applies, so a set that could not be graded is never stored.
    """
    case_set = CaseSet(module, function, tuple(HeldCase.from_mapping(item) for item in cases))
    target = case_path(module, function, home=home)
    _ensure_home(target.parent)
    data = (case_set.to_json() + "\n").encode("utf-8")
    if len(data) > _MAX_CASE_FILE_BYTES:
        raise OracleError("oracle case set exceeds its size bound")
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class LoadedCaseSet:
    """A case set read by the evaluator, with the digest of its bytes."""

    path: Path
    sha256: str
    case_set: CaseSet


def _read_bounded(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise OracleUnavailable(f"oracle case set cannot be opened ({type(exc).__name__})") from exc
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OracleUnavailable("oracle case set is not a regular file")
        data = stream.read(_MAX_CASE_FILE_BYTES + 1)
    if len(data) > _MAX_CASE_FILE_BYTES:
        raise OracleUnavailable("oracle case set exceeds its size bound")
    return data


def load_case_set(module: str, function: str) -> LoadedCaseSet | None:
    """Read the held set for ``module.function``; ``None`` when none is held.

    Raises ``OracleUnavailable`` for a set that exists but is unsafe or
    malformed, and for one that names a different target.
    """
    path = case_path(module, function)
    try:
        data = _read_bounded(path)
    except FileNotFoundError:
        return None
    try:
        case_set = CaseSet.parse(json.loads(data.decode("utf-8")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise OracleUnavailable(f"oracle case set is malformed ({exc})") from None
    if (case_set.module, case_set.function) != (module, function):
        raise OracleUnavailable("oracle case set names a different target")
    return LoadedCaseSet(path, hashlib.sha256(data).hexdigest(), case_set)


def current_digest(path: Path) -> str | None:
    """SHA-256 of the case file as it is now, or ``None`` if it is unreadable."""
    try:
        return hashlib.sha256(_read_bounded(Path(path))).hexdigest()
    except (OSError, OracleUnavailable):
        return None


def confidentiality(path: Path) -> tuple[bool, str]:
    """Whether the selected supervisor keeps the case file from the candidate.

    Only the Linux uid supervisor bounds reads.  There, the file and its
    directory must be neither readable nor writable by the candidate uid.
    """
    from scripts import selfmod_linux_isolation as linux

    _runner, kind = linux.candidate_supervisor()
    if kind != LINUX_UID:
        return False, f"the {kind} candidate supervisor does not bound candidate reads"
    try:
        linux.require_not_candidate_readable([path])
        linux.require_not_candidate_writable([path])
    except linux.LinuxIsolationUnavailable as exc:
        return False, f"held expected values are exposed to the candidate uid: {str(exc)[:300]}"
    return True, "held expected values are closed to the candidate uid"


# Evaluator code only: ``-I`` keeps the candidate checkout (the cwd) off
# sys.path, so no candidate byte runs in this process.  It runs as the
# candidate uid and must be refused by the kernel on both attempts.
_SEAL_PROBE = r'''
import errno
import os
import sys

target = sys.argv[1]
results = []
for label, attempt in (
    ("read", lambda: os.close(os.open(target, os.O_RDONLY | os.O_NOFOLLOW))),
    ("list", lambda: os.listdir(os.path.dirname(target))),
):
    try:
        attempt()
    except PermissionError:
        results.append(label + "=denied")
        continue
    except OSError as exc:
        results.append("%s=error:%s" % (label, errno.errorcode.get(exc.errno, exc.errno)))
        continue
    results.append(label + "=ALLOWED")
print("SELFMOD ORACLE SEAL " + " ".join(results), flush=True)
raise SystemExit(0 if all(item.endswith("=denied") for item in results) else 3)
'''


def prove_read_denied(path: Path, cwd: Path, timeout: int, *,
                      python: str | None = None) -> tuple[bool, str]:
    """Prove at the OS boundary that the candidate uid cannot read the set.

    Runs :data:`_SEAL_PROBE` through the selected supervisor as the
    candidate uid, with the case file as protected truth, and accepts only
    a supervisor-attested ``linux-uid`` pass whose output reports both the
    open and the directory listing as ``EACCES``.
    """
    from scripts import selfmod_linux_isolation as linux
    from scripts.selfmod_host_grader import attested_pass

    run_isolated, kind = linux.candidate_supervisor()
    if kind != LINUX_UID:
        return False, f"the {kind} candidate supervisor does not bound candidate reads"
    command = [python or sys.executable, "-I", "-c", _SEAL_PROBE, str(path)]
    try:
        result = run_isolated(
            command, cwd=cwd, timeout=max(1, min(int(timeout), _SEAL_TIMEOUT_SECONDS)),
            protected_paths=(path,),
        )
    except (OSError, RuntimeError) as exc:
        return False, f"read-denial probe could not run ({str(exc)[:200]})"
    output = str(result.get("output") or "") if isinstance(result, dict) else ""
    seal = [line for line in output.splitlines() if line.startswith(SEAL_PREFIX)]
    if not attested_pass(result, kind) or seal != [_SEAL_DENIED]:
        return False, "candidate uid was not refused the held expected values: %s" % (
            seal[-1] if seal else "no seal report")
    return True, "candidate uid received EACCES reading the held expected values"


# Candidate-side probe.  It imports and calls candidate code, so everything it
# prints is candidate-controlled; the parent trusts none of it except as raw
# outputs to compare with outcomes the candidate cannot read.
_ORACLE_PROBE = r'''
import importlib
import json
import pathlib
import sys
import tempfile
import uuid

payload = json.loads(sys.argv[1])
root = pathlib.Path(payload["root"]).resolve()
sys.pycache_prefix = str(pathlib.Path(tempfile.gettempdir()) / ("sonder-oracle-" + uuid.uuid4().hex))
sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
module = importlib.import_module(payload["module"])
origin = pathlib.Path(module.__file__).resolve()
if root not in origin.parents:
    raise SystemExit(2)
function = getattr(module, payload["function"])
outputs = {}
for case in payload["cases"]:
    try:
        value = function(*case["args"], **case["kwargs"])
    except Exception as exc:
        outputs[case["token"]] = {"raised": type(exc).__name__}
        continue
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        outputs[case["token"]] = {"unencodable": type(value).__name__}
    else:
        outputs[case["token"]] = {"value": value}
sys.stdout.write(%(prefix)r + json.dumps(
    {"nonce": payload["nonce"], "outputs": outputs}, sort_keys=True, allow_nan=False,
) + "\n")
sys.stdout.flush()
''' % {"prefix": ORACLE_FRAME_PREFIX}


def challenge_command(workspace: Path, challenge: OracleChallenge, case_set: CaseSet, *,
                      python: str | None = None) -> list[str]:
    """argv for the candidate-side probe: inputs and nonce, never outcomes."""
    payload = challenge_payload(challenge, case_set, root=str(Path(workspace).resolve()))
    return [python or sys.executable, "-I", "-c", _ORACLE_PROBE, payload]


def _inspect(module: str, function: str) -> dict[str, object]:
    loaded = load_case_set(module, function)
    if loaded is None:
        return {"path": str(case_path(module, function)), "held": False}
    confidential, reason = confidentiality(loaded.path)
    return {"path": str(loaded.path), "held": True, "sha256": loaded.sha256,
            "cases": len(loaded.case_set.cases), "confidential": confidential,
            "confidentiality": reason}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("provision", "inspect"):
        sub = commands.add_parser(name)
        sub.add_argument("--module", required=True)
        sub.add_argument("--function", required=True)
        if name == "provision":
            sub.add_argument("--cases", required=True, type=Path,
                             help="JSON list of held cases (delete it afterwards)")
    args = parser.parse_args(argv)
    try:
        if args.command == "provision":
            cases = json.loads(args.cases.read_text(encoding="utf-8"))
            if not isinstance(cases, list):
                raise OracleError("the cases file must hold a JSON list")
            digest = provision(args.module, args.function, cases)
            print(json.dumps({"path": str(case_path(args.module, args.function)),
                              "sha256": digest, "version": CASE_SET_VERSION}))
        else:
            print(json.dumps(_inspect(args.module, args.function), sort_keys=True))
    except (OSError, ValueError, OracleUnavailable) as exc:
        print(f"selfmod oracle: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
