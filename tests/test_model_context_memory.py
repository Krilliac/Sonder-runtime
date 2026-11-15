import pytest

from sonder_runtime.platform import declared_context, model_context_memory as mcm


@pytest.mark.parametrize("name, key", [
    ("X", "x:latest"),
    ("x:latest", "x:latest"),
    ("Qwen:7B", "qwen:7b"),
    ("host:5000/model", "host:5000/model:latest"),
    ("host:5000/model:tag", "host:5000/model:tag"),
    ("", ""),
    (None, ""),
])
def test_model_key(name, key):
    assert mcm.model_key(name) == key


def test_failure_replays_last_success_and_keeps_declared():
    memory = mcm.ModelContextMemory()
    assert memory.settle("x", 8192, 262144, "7B") == (262144, "7B")
    assert memory.settle("x:latest", False, None, None) == (262144, "7B")
    assert memory.declared_for("x") == 8192
    assert memory.replay("x", None, None) == (262144, "7B")


def test_success_without_declaration_clears_it_and_failure_without_history_is_empty():
    memory = mcm.ModelContextMemory()
    assert memory.settle("y", False, None, None) == (None, None)
    memory.settle("y", 4096, 8192, "1B")
    memory.settle("y", None, 8192, "1B")
    assert memory.declared_for("y") is None


def test_declared_from_show_tolerates_non_dict():
    assert declared_context.declared_from_show(None) is None
    assert declared_context.declared_from_show({"parameters": "num_ctx 4096"}) == 4096
