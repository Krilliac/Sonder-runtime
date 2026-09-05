"""Explicit recovery controls composed only by the owning runtime."""

from dataclasses import asdict
from types import SimpleNamespace
import time
import uuid

from ..adapters.host_terminal_projection import TerminalProjectionCodec
from ..adapters.persistence.terminal_output import SQLiteTerminalOutputStore
from ..application.ports.app_control import identifier
from ..application.ports.app_control_http import ControlError
from ..platform.runtime_threads import ThreadPoolExecutor
from .app_recovery_coordinator import AppWorkRecoveryAttempt
from .app_work_recovery_registry import AppWorkRecoveryRegistry
from .managed_standalone import ManagedStandaloneRecovery


def public_recovery(snapshot, work_serializer):
    result = {
        key: snapshot[key] for key in ("attempt_id", "work_id", "phase", "code", "busy")
    }
    if snapshot["approval"] is not None:
        result["approval"] = asdict(snapshot["approval"])
    if snapshot["work"] is not None:
        result["work"] = work_serializer(snapshot["work"])
    return result


def install_owned_recovery_http(work):
    from .managed_app_work import (
        register_owned_app_recovery,
        require_owned_app_recovery,
    )

    if getattr(work, "_recovery_binding", None) is not None:
        raise PermissionError("recovery HTTP is already installed")
    result = AppWorkRecoveryHttpBinding(
        work,
        register_owned=register_owned_app_recovery,
        require_owned=require_owned_app_recovery,
    )
    work._recovery_binding = result
    return result


def current_recovery_http(work):
    work.require_current()
    result = getattr(work, "_recovery_binding", None)
    if result is None:
        return None
    if type(result) is not AppWorkRecoveryHttpBinding or result.work is not work:
        raise PermissionError("exact installed recovery HTTP required")
    result.require_current()
    return result


class AppWorkRecoveryHttpBinding:
    def __init__(self, work, *, register_owned, require_owned):
        if not callable(register_owned) or not callable(require_owned):
            raise TypeError("owned recovery registration required")
        self.work, self.control = work, work.control
        self._require_owned = require_owned
        work.require_current()
        self.registry = AppWorkRecoveryRegistry(
            application=work.application,
            authority=work.authority,
            attempt_factory=self._attempt,
            executor=ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="app-recovery"
            ),
        )
        registration = None
        try:
            registration = register_owned(work.application, self.registry)
            self.require_current()
            registration.commit()
        except BaseException:
            if registration is not None:
                registration.rollback(timeout=2)
            else:
                self.registry.close()
            raise

    def require_current(self):
        inventory = self.work.require_current()
        if self._require_owned(self.work.application) is not self.registry:
            raise PermissionError("exact owned recovery registry unavailable")
        return inventory

    def _attempt(self, selection):
        work = self.work

        def guard():
            inventory = self.require_current()
            work.authority.work_atomic(selection, selection.context, lambda tx: None)
            return inventory

        def approve(prepared, context):
            guard()
            return work._bridge(guard).authorize(
                "workspace_run",
                prepared.approval_payload(),
                surface="app-control",
                expires_at=min(
                    selection.control.expires_at,
                    time.time() + context.remaining_seconds,
                ),
            )

        def private_paths():
            # Only this fresh guard invocation supplies these paths; no snapshot
            # is retained across callbacks or used in place of account admission.
            return guard().admission_directories

        model_roots = work.model_roots

        def recovery(selected, record):
            guard()
            output = SQLiteTerminalOutputStore(
                work.output_root, model_writable_roots=model_roots
            )

            def context():
                guard()
                return selected.context

            codec = TerminalProjectionCodec(output_store=output, output_context=context)
            host = work.authority.continuation_service(selected, projection_codec=codec)
            return ManagedStandaloneRecovery(
                controller=SimpleNamespace(run_id="recovery-" + uuid.uuid4().hex),
                application=work.application,
                host=host,
                context=selected.context,
                host_conversation_id=selected.host_conversation_id,
                private_paths=private_paths,
                model_writable_roots=model_roots,
                approve_attachment=approve,
                approve_verification=approve,
            )

        return AppWorkRecoveryAttempt(
            authority=work.authority,
            selection=selection,
            application=work.application,
            recovery_factory=recovery,
            verifier_factory=work.runtime._standalone_verifier_factory,
            approve_attachment=approve,
            approve_verification=approve,
            private_paths=private_paths,
            model_writable_roots=model_roots,
        )

    def perform(self, action, payload, *, account_token, control_token, publish):
        selection = None
        held = False
        publication_started = False

        def once(*args):
            nonlocal publication_started
            if publication_started:
                raise PermissionError("recovery response already attempted")
            publication_started = True
            return publish(*args)

        try:
            self.require_current()
            fields = (
                {"work_id", "attachment_command_id", "completion_command_id"}
                if action == "prepare_recovery"
                else {"attempt_id"}
            )
            if (
                action
                not in {
                    "prepare_recovery",
                    "inspect_recovery",
                    "attach_recovery",
                    "resume_recovery",
                    "close_recovery",
                }
                or type(payload) is not dict
                or set(payload) != fields
            ):
                raise ValueError("exact explicit recovery request required")
            for value in payload.values():
                identifier(value)
            selection = self.work._issue(account_token, control_token, action, payload)
            with self.work._account_lock:
                principal = selection.control.principal_id
                if (
                    self.work._account_active.get(principal, 0) >= 2
                    or len(self.work._account_active) >= 64
                ):
                    raise ControlError(429, "APP_RECOVERY_BUSY")
                self.work._account_active[principal] = (
                    self.work._account_active.get(principal, 0) + 1
                )
                held = True
            if action == "prepare_recovery":
                snapshot = self.registry.prepare(selection, **payload)
            elif action == "inspect_recovery":
                snapshot = self.registry.inspect(selection, payload["attempt_id"])
            else:
                snapshot = self.registry.act(
                    selection, payload["attempt_id"], action.removesuffix("_recovery")
                )
            result = (
                200 if action == "inspect_recovery" else 202,
                dict(
                    ok=True, recovery=public_recovery(snapshot, self.work.public_record)
                ),
            )
        except Exception as error:
            result = self.control._error(error)
        if selection is not None:
            try:
                self.work.authority.release_selection(selection)
            except PermissionError:
                pass  # Exact retained worker lease or already closed selection.
        try:
            if selection is None:
                once(*result)
            else:
                try:
                    self.work._publish(
                        selection, account_token, control_token, result, once
                    )
                except Exception as error:
                    if publication_started:
                        raise
                    once(*self.control._error(error))
        finally:
            if held:
                with self.work._account_lock:
                    self.work._account_active[principal] -= 1
                    if not self.work._account_active[principal]:
                        del self.work._account_active[principal]
