from collections import OrderedDict

import pytest

import sonder_doctor
from sonder_runtime.domain import doctor_specs


def test_doctor_specs_has_packaged_ownership_and_legacy_delegate():
    assert sonder_doctor._iter_specs is doctor_specs.iter_specs
    assert sonder_doctor.CheckCallable is doctor_specs.CheckCallable


def test_iter_specs_preserves_mapping_order_and_stringifies_names():
    first = lambda: "ok"
    second = lambda: "warn"

    assert doctor_specs.iter_specs(OrderedDict([(1, first), ("two", second)])) == [
        ("1", first),
        ("two", second),
    ]


def test_iter_specs_names_bare_callables_and_fallback():
    def named_check():
        return "ok"

    class CallableCheck:
        name = "custom"

        def __call__(self):
            return "ok"

    anonymous = type("CallableWithoutName", (), {"__call__": lambda self: "ok"})()
    specs = doctor_specs.iter_specs([named_check, CallableCheck(), anonymous])

    assert [name for name, _fn in specs] == [
        "named_check",
        "custom",
        "check_2",
    ]


def test_iter_specs_accepts_pairs_and_rejects_invalid_entries():
    check = lambda: "ok"
    assert doctor_specs.iter_specs([("pair", check)]) == [("pair", check)]

    with pytest.raises(TypeError, match="check spec"):
        doctor_specs.iter_specs(["not callable"])
