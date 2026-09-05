"""Private live REPL conversation ownership; turns never become capabilities."""

from threading import RLock

from ..application.agents.host_turns import (
    advance_host_turn,
    capture_host_turn,
    capture_host_final,
    close_host_turn,
)
from ..adapters.agent_terminal_evidence import HostObservationLedger
from ..interfaces.standalone_agent_lanes import HostTerminalDraft
from .managed_standalone import ManagedStandaloneSession
from ..application.ports.host_final import HostFinalFacts


class ManagedConversationLifetime:
    def __init__(self, *, application, session_factory, require_current):
        if not callable(session_factory) or not callable(require_current):
            raise TypeError("trusted live conversation callbacks required")
        self._application, self._factory, self._guard = (
            application,
            session_factory,
            require_current,
        )
        self._lock = RLock()
        self._owner = self._active = self._previous = None
        self._closed = False
        self._released = False

    @property
    def context(self):
        with self._lock:
            self._require_current()
            if self._owner is None:
                raise PermissionError("conversation has not admitted its first turn")
            return self._owner.context

    def _require_current(self):
        if self._closed:
            raise PermissionError("host conversation lifetime is closed")
        self._guard()

    def factory(self, controller, application):
        with self._lock:
            self._require_current()
            if application is not self._application or self._active is not None:
                raise PermissionError("exact idle host conversation required")
            if self._owner is None:
                owner = self._factory(controller, application)
                if type(owner) is not ManagedStandaloneSession:
                    raise TypeError("private managed root owner required")
                self._owner = owner
            owner = self._owner
            owner.require_current()
            verifier = (
                self._previous._session._verifier
                if self._previous is not None
                else None
            )
            admission = advance_host_turn(
                owner._bound, controller.run_id, verifier=verifier
            )
            view = _ManagedTurn(self, controller, admission)
            self._active = view
            try:
                view._compose(owner)
                return view
            except BaseException:
                self._active = None
                # An admitted turn with failed composition remains unknown.
                raise

    def finalize_result(self, result):
        """The trusted outer REPL return, after reporting, ends the turn.

        Controller cleanup already fenced the view. Only this private owner
        can persist its staged facts with the actual outward text and close
        the durable turn; no model dispatch or new controller is admitted.
        """
        with self._lock:
            self._require_current()
            view = self._active
            if view is None:
                return result
            if not getattr(view, '_awaiting_final', False) or not isinstance(result, str):
                raise PermissionError('outer host final boundary unavailable')
            capture_host_final(view._session._bound, view._admission, result,
                view._final_facts, HostObservationLedger.restore(view._draft.ledger_bytes))
            close_host_turn(view._session._bound, view._admission)
            self._previous, self._active = view, None
            return result

    def close(self):
        with self._lock:
            if self._released:
                return
            # Fence first, but retain the exact owner until cleanup succeeds.
            # A failed release must remain explicitly retryable.
            self._closed = True
            if self._active is not None:
                self._active._closed = True
                self._active = None
            if self._owner is not None:
                self._owner.close()
            self._released = True


class ReplConversationSlot:
    """One private launcher selection, scoped to one actual REPL invocation."""

    def __init__(self):
        self._lock = RLock()
        self._identity = self._invoke = self._close = None
        self._closing = False

    def select(self, identity):
        with self._lock:
            if self._closing:
                raise PermissionError("private REPL cleanup requires explicit retry")
            if self._identity != identity:
                self.clear()
                self._identity = identity
            return self._invoke is not None

    def install(self, invoke, close):
        with self._lock:
            if self._closing or self._invoke is not None or not callable(invoke) or not callable(close):
                raise PermissionError("private REPL selection already owns a lifetime")
            self._invoke, self._close = invoke, close

    def run(self, callback):
        with self._lock:
            if self._closing or self._invoke is None:
                raise PermissionError("private REPL lifetime unavailable")
            return self._invoke(callback)

    def clear(self):
        with self._lock:
            close = self._close
            self._closing = True
            if close is not None:
                close()
            self._identity = self._invoke = self._close = None
            self._closing = False


class _ManagedTurn:
    def __init__(self, lifetime, controller, admission):
        self._lifetime, self._controller, self._admission = (
            lifetime,
            controller,
            admission,
        )
        self._session = None
        self._closed = False

    def _guard(self):
        if self._closed or self._lifetime._active is not self:
            raise PermissionError("host turn has been fenced")
        self._lifetime._require_current()

    def _compose(self, owner):
        # A separate turn session binds the new exact controller issuer. The
        # original root session/controller is never overwritten or reconstructed.
        if (
            self._admission.owner is not owner._bound
            or self._admission.run_id != self._controller.run_id
        ):
            raise PermissionError("private turn admission changed")
        session = ManagedStandaloneSession.__new__(ManagedStandaloneSession)
        session._controller, session._application = self._controller, owner._application
        session._host, session._host_id, session._approve = (
            owner._host,
            owner._host_id,
            owner._approve,
        )
        session._issuer = object()
        session._closed = session._recovered = False
        session._verifier = session._publisher = session.published_terminal = None
        session._bound, session._admission = owner._bound, owner._admission
        session.parent_session_id = owner.parent_session_id
        session._turn_guard = self._guard
        session._host.command_codec = session
        session._host.terminal_result_codec = None
        self._session = session

    @property
    def context(self):
        self.require_current()
        return self._session.context

    def require_current(self):
        with self._lifetime._lock:
            self._guard()
            self._session.require_current()

    def inherit_host_ledger(self, ledger):
        self.require_current()
        previous = self._admission.previous_projection
        if previous is None:
            return ledger
        retained = HostObservationLedger.restore(previous.ledger_bytes)
        if retained._scope != ledger._scope:
            raise PermissionError("host turn project scope changed")
        return retained

    def capture_terminal(self, draft):
        with self._lifetime._lock:
            self.require_current()
            if type(draft) is not HostTerminalDraft:
                raise TypeError("exact host terminal draft required")
            ledger = HostObservationLedger.restore(draft.ledger_bytes)
            capture_host_turn(self._session._bound, self._admission, draft, ledger)
            self._draft = draft

    def capture_final(self, output, facts):
        with self._lifetime._lock:
            self.require_current()
            draft = getattr(self, '_draft', None)
            if draft is None:
                raise PermissionError('original host terminal evidence unavailable')
            capture_host_final(self._session._bound, self._admission, output, facts,
                               HostObservationLedger.restore(draft.ledger_bytes))

    def stage_final(self, facts):
        with self._lifetime._lock:
            self.require_current()
            if type(facts) is not HostFinalFacts or getattr(self, '_draft', None) is None:
                raise PermissionError('typed final facts and original draft required')
            previous = getattr(self, '_final_facts', None)
            if previous is not None and previous != facts:
                raise PermissionError('staged final facts are immutable')
            self._final_facts = facts

    def dispatch(self, prepared):
        with self._lifetime._lock:
            self.require_current()
            return self._session.dispatch(prepared)

    def report_metadata(self):
        with self._lifetime._lock:
            self.require_current()
            return dict(
                self._session.report_metadata(), host_turn=self._admission.ordinal
            )

    def request_cancel(self):
        with self._lifetime._lock:
            self.require_current()
            self._session.request_cancel()

    def verify_delegated(self, draft, *, verifier_factory):
        with self._lifetime._lock:
            self.require_current()
            return self._session.verify_delegated(
                draft, verifier_factory=verifier_factory
            )

    def close(self):
        with self._lifetime._lock:
            if self._closed:
                return
            if getattr(self, '_final_facts', None) is not None:
                self.require_current()
                self._closed = True
                self._awaiting_final = True
                return
            try:
                self.require_current()
                close_host_turn(self._session._bound, self._admission)
            finally:
                self._closed = True
                self._lifetime._previous = self
                self._lifetime._active = None
