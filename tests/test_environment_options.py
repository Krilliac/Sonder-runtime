from sonder_runtime.platform.environment_options import env_int_option
import server


def test_missing_option_returns_default():
    assert env_int_option("MISSING", 17, environ={}) == 17


def test_disable_tokens_return_none():
    for value in ("", "auto", " default ", "none", "off"):
        assert env_int_option("OPTION", 17, environ={"OPTION": value}) is None


def test_integer_is_trimmed_and_parsed():
    assert env_int_option("OPTION", 17, environ={"OPTION": "  -12 "}) == -12


def test_invalid_integer_returns_default():
    assert env_int_option("OPTION", 17, environ={"OPTION": "wat"}) == 17


def test_server_alias_preserves_identity():
    assert server._env_int_option is env_int_option


def test_server_options_read_live_environment(monkeypatch):
    monkeypatch.setenv("SONDER_NUM_THREAD", " 9 ")
    assert server._local_model_options(0.2, 64, 2048)["num_thread"] == 9


def test_server_alias_reads_process_environment(monkeypatch):
    monkeypatch.setenv("SONDER_NUM_BATCH", "256")
    assert server._env_int_option("SONDER_NUM_BATCH", 512) == 256
