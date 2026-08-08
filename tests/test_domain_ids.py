from sonder_runtime.domain.common.ids import is_id, new_id


def test_is_id_rejects_non_hex_payloads():
    assert is_id(new_id("run"), "run")
    assert not is_id("run_" + "z" * 32, "run")
    assert not is_id("run_" + "!" * 32, "run")
