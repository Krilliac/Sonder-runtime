"""Bounded runtime-owned explicit recovery, with no action replay on ambiguity."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
from threading import RLock, Lock

from ..application.ports.app_control import CommandConflict, NotFound, identifier
from ..application.ports.app_control_http import ControlError
from .app_recovery_coordinator import AppWorkRecoveryAttempt, AppRecoveryView
from .app_work_recovery import AppWorkRecoveryHistory


@dataclass
class _Entry:
    identity: str
    selection: object = field(repr=False)
    inputs: tuple
    lease: object = field(repr=False)
    attempt: object = field(default=None, repr=False)
    prepared: object = field(default=None, repr=False)
    work: object = field(default=None, repr=False)
    phase: str = "preparing"
    code: str = "PREPARATION_ACCEPTED"
    approval: object = None
    busy: bool = True
    uncertain: bool = False
    released: bool = False


class AppWorkRecoveryRegistry:
    def __init__(
        self, *, application, authority, attempt_factory, executor, max_attempts=32
    ):
        if (
            application is None
            or authority is None
            or not callable(attempt_factory)
            or not isinstance(executor, ThreadPoolExecutor)
            or type(max_attempts) is not int
            or not 1 <= max_attempts <= 32
        ):
            raise TypeError("exact private bounded recovery composition required")
        self._application, self._authority = application, authority
        self._factory, self._executor = attempt_factory, executor
        self._limit, self._lock = max_attempts, RLock()
        self._close_lock = Lock()
        self._entries = {}
        self._active = False
        self._closed = False

    @property
    def application(self):
        return self._application

    def _live(self, selection):
        self._authority.work_atomic(selection, selection.context, lambda tx: None)

    @staticmethod
    def _scope(selection):
        return (selection.account, selection.control, selection.binding, selection.slot)

    def _entry(self, selection, attempt_id):
        self._live(selection)
        identifier(attempt_id)
        with self._lock:
            if self._closed:
                raise ControlError(503, "APP_RECOVERY_UNAVAILABLE")
            entry = self._entries.get(attempt_id)
            if entry is None or self._scope(entry.selection) != self._scope(selection):
                raise NotFound("recovery attempt unavailable")
            return entry

    def prepare(
        self, selection, *, work_id, attachment_command_id, completion_command_id
    ):
        self._live(selection)
        inputs = (work_id, attachment_command_id, completion_command_id)
        for value in inputs:
            identifier(value)
        identity = hashlib.sha256(
            json.dumps(
                (
                    selection.control.principal_id,
                    selection.control.control_session_id,
                    selection.binding.binding_id,
                    selection.binding.revision,
                    selection.slot.selection_id,
                    selection.slot.epoch,
                    *inputs,
                ),
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        # Reserve current selection before publishing a callback or receipt.
        lease = self._authority.retain_selection(selection)
        retained = False
        try:
            with self._lock:
                if self._closed:
                    raise ControlError(503, "APP_RECOVERY_UNAVAILABLE")
                prior = self._entries.get(identity)
                if prior is not None:
                    if self._scope(prior.selection) != self._scope(selection):
                        raise CommandConflict("recovery authority changed")
                    return self._snapshot(prior)
                if self._active or len(self._entries) >= self._limit:
                    raise ControlError(429, "APP_RECOVERY_BUSY")
                entry = _Entry(identity, selection, inputs, lease)
                self._entries[identity] = entry
                self._active = retained = True
            self._submit(entry, "prepare")
            return self._snapshot(entry)
        finally:
            if not retained:
                self._authority.release_retained(lease)

    def act(self, selection, attempt_id, action):
        entry = self._entry(selection, attempt_id)
        if action not in {"attach", "resume", "close"}:
            raise ValueError("explicit recovery action required")
        with self._lock:
            if entry.busy:
                return self._snapshot(entry)
            if (
                entry.phase == "closed"
                or entry.phase == "terminal"
                and action != "close"
            ):
                return self._snapshot(entry)
            if entry.uncertain and action != "close":
                raise CommandConflict("unknown recovery action requires inspection")
            allowed = {
                "attach": {"prepared", "attachment_pending"},
                "resume": {"attached", "approval_pending"},
            }
            if action != "close" and entry.phase not in allowed[action]:
                raise CommandConflict("explicit recovery phase differs")
            if self._active:
                raise ControlError(429, "APP_RECOVERY_BUSY")
            self._active = entry.busy = True
            entry.code = "ACTION_ACCEPTED"
            entry.approval = None
        self._submit(entry, action)
        return self._snapshot(entry)

    def _submit(self, entry, action):
        try:
            self._executor.submit(self._run, entry, action)
        except BaseException:
            # submit can enqueue and then lose its response. Do not assume no
            # callback exists, release capacity, or attempt another action.
            with self._lock:
                entry.uncertain = True
                entry.phase, entry.code = "unknown", "SUBMISSION_OUTCOME_UNKNOWN"

    def _run(self, entry, action):
        try:
            with self._lock:
                if self._closed or (entry.uncertain and action != "close"):
                    return
            if action == "close":
                self._release(entry)
                with self._lock:
                    entry.phase, entry.code = "closed", "LOCAL_HANDLE_CLOSED"
                return
            self._live(entry.selection)
            if action == "prepare":
                attempt = self._factory(entry.selection)
                if (
                    type(attempt) is not AppWorkRecoveryAttempt
                    or attempt._application is not self.application
                    or attempt._authority is not self._authority
                    or attempt._selection is not entry.selection
                ):
                    raise PermissionError("exact owned recovery attempt required")
                entry.attempt = attempt
                self._authority.release_retained(entry.lease)
                entry.lease = None
                prepared = attempt.prepare(
                    work_id=entry.inputs[0],
                    attachment_command_id=entry.inputs[1],
                    completion_command_id=entry.inputs[2],
                )
                with self._lock:
                    entry.prepared, entry.work = prepared, prepared.work
                    entry.phase, entry.code = "prepared", "EXPLICIT_ATTACHMENT_REQUIRED"
            else:
                result = getattr(entry.attempt, action)(entry.prepared)
                if type(result) is not AppRecoveryView:
                    raise TypeError("exact recovery view required")
                with self._lock:
                    entry.work, entry.phase, entry.code = (
                        result.work,
                        result.phase,
                        result.code,
                    )
                    entry.approval = result.approval
        except BaseException:
            with self._lock:
                entry.uncertain = True
                entry.phase, entry.code = "unknown", "ACTION_OUTCOME_UNKNOWN"
        finally:
            with self._lock:
                self._active = entry.busy = False

    def inspect(self, selection, attempt_id):
        entry = self._entry(selection, attempt_id)
        # Reading durable scoped completion never enters attachment, approval,
        # verifier, publisher, or an uncertain callback again.
        current = AppWorkRecoveryHistory(self._authority).inspect(
            selection, work_id=entry.inputs[0]
        )
        with self._lock:
            if current is not None:
                if entry.work is not None and current.prepared != entry.work.prepared:
                    raise CommandConflict("original recovery work changed")
                entry.work = current
                if (
                    not entry.busy
                    and current.state == "terminal"
                    and current.completion is not None
                    and current.completion.phase == "certified_after_return"
                ):
                    entry.phase, entry.code = "terminal", "DURABLE_COMPLETION_OBSERVED"
            return self._snapshot(entry)

    def _snapshot(self, entry):
        with self._lock:
            return dict(
                attempt_id=entry.identity,
                work_id=entry.inputs[0],
                phase=entry.phase,
                code=entry.code,
                busy=entry.busy,
                approval=entry.approval,
                work=entry.work,
            )

    def _release(self, entry):
        if entry.released:
            return
        if entry.lease is not None:
            self._authority.release_retained(entry.lease)
            entry.lease = None
        if entry.attempt is not None:
            entry.attempt.close()
        else:
            self._authority.release_selection(entry.selection)
        entry.released = True

    def stop_admissions(self):
        with self._lock:
            self._closed = True

    def close(self):
        with self._close_lock:
            self._close()

    def _close(self):
        # Called in the slot's retained owned helper. The slot's one deadline
        # bounds waiting; timeout never means this synchronous drain completed.
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            entries = tuple(self._entries.values())
        failed = False
        for entry in entries:
            try:
                self._release(entry)
            except BaseException:
                failed = True
        if failed:
            raise RuntimeError("recovery cleanup remains unresolved")
