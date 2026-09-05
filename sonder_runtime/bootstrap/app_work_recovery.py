"""Private current-account observation of original app work; no execution."""


class AppWorkRecoveryHistory:
    def __init__(self, authority):
        self._authority = authority

    def inspect(self, selection, *, work_id):
        def read(tx):
            return tx.read_recovery_work(
                principal_id=selection.binding.principal_id,
                control_session_id=selection.control.control_session_id,
                binding_id=selection.binding.binding_id,
                binding_revision=selection.binding.revision,
                selection_id=selection.slot.selection_id,
                epoch=selection.slot.epoch,
                work_id=work_id,
            )

        return self._authority.work_atomic(selection, selection.context, read)
