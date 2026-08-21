from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter as LegacyUnitOfWork
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.bootstrap import app as bootstrap_app


def test_unit_of_work_has_one_canonical_packaged_owner():
    assert LegacyUnitOfWork is UnitOfWorkAdapter


def test_bootstrap_uses_canonical_unit_of_work_owner():
    application = bootstrap_app.build_application()
    assert application.unit_of_work is UnitOfWorkAdapter
