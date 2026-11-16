"""SONDER_SECURITY_EVAL_* parsing: every key, narrow defaults on a bad value, folded into SonderConfig."""
from __future__ import annotations

import os

import pytest

from sonder_runtime.platform.config import ConfigError, SonderConfig, load_config
from sonder_runtime.platform.security_eval_config import (
    SecurityEvalConfig, security_eval_config_from_env)

pytestmark = pytest.mark.unit

P = "SONDER_SECURITY_EVAL_"


def parse(**keys):
    errors: list[str] = []
    config = security_eval_config_from_env({P + key: value for key, value in keys.items()}, errors)
    return config, errors


def test_defaults_are_the_narrow_values():
    config, errors = parse()
    assert errors == [] and config == SecurityEvalConfig()
    assert config.dataset == "" and config.repo_roots == () and config.context == "both"
    assert config.num_ctx is None and config.think is None and config.allow_remote_ollama is False
    assert (config.num_predict, config.temperature, config.item_timeout_seconds) == (768, 0.0, 300)
    assert config.call_budget_seconds == 1500 and config.keep_raw_chars == 0


def test_every_key_in_the_design_table_is_parsed(tmp_path):
    roots = os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
    config, errors = parse(
        DATASET=str(tmp_path / "set.jsonl"), REPO_ROOTS=roots, MODEL="demo:9b", CONTEXT="retrieved",
        TEMPLATE_FILE=str(tmp_path / "t.txt"), NUM_CTX="8192", NUM_PREDICT="512", TEMPERATURE="0.3",
        THINK="off", ITEM_TIMEOUT_SECONDS="120", CALL_BUDGET_SECONDS="600", ALLOW_REMOTE_OLLAMA="on",
        SOURCE_PAD_LINES="10", MAX_CONTEXT_CHARS="4000", MAX_IDENTIFIERS="8", MAX_HITS_PER_KIND="2",
        SNIPPET_LINES="1", COMMON_HIT_LIMIT="50", INDEX_MAX_FILES="500", INDEX_MAX_BYTES="1048576",
        MAX_FILE_BYTES="65536", INDEX_TIMEOUT_SECONDS="30", MAX_DATASET_BYTES="2097152",
        MAX_RECORDS="900", KEEP_RAW_CHARS="200", CHUNK_CHARS="8000", MATCH_LINE_TOLERANCE="3")
    assert errors == []
    assert config.dataset.endswith("set.jsonl") and config.model == "demo:9b" and config.context == "retrieved"
    assert config.repo_roots == (str(tmp_path / "a"), str(tmp_path / "b"))
    assert config.template_file.endswith("t.txt") and config.num_ctx == 8192
    assert (config.num_predict, config.temperature, config.think) == (512, 0.3, False)
    assert (config.item_timeout_seconds, config.call_budget_seconds, config.allow_remote_ollama) == (120, 600, True)
    assert (config.source_pad_lines, config.max_context_chars, config.max_identifiers) == (10, 4000, 8)
    assert (config.max_hits_per_kind, config.snippet_lines, config.common_hit_limit) == (2, 1, 50)
    assert (config.index_max_files, config.index_max_bytes, config.max_file_bytes) == (500, 1048576, 65536)
    assert (config.index_timeout_seconds, config.max_dataset_bytes, config.max_records) == (30, 2097152, 900)
    assert (config.keep_raw_chars, config.chunk_chars, config.match_line_tolerance) == (200, 8000, 3)
    assert parse(THINK="on")[0].think is True


@pytest.mark.parametrize("key,value", [
    ("CONTEXT", "everything"), ("MODEL", "bad model name!"), ("NUM_CTX", "12"), ("NUM_CTX", "lots"),
    ("NUM_PREDICT", "63"), ("NUM_PREDICT", "9000"), ("TEMPERATURE", "1.5"), ("TEMPERATURE", "warm"),
    ("THINK", "maybe"), ("ITEM_TIMEOUT_SECONDS", "5"), ("CALL_BUDGET_SECONDS", "99999"),
    ("SOURCE_PAD_LINES", "201"), ("MAX_CONTEXT_CHARS", "100"), ("MAX_IDENTIFIERS", "0"),
    ("MAX_HITS_PER_KIND", "21"), ("SNIPPET_LINES", "21"), ("COMMON_HIT_LIMIT", "9"),
    ("INDEX_MAX_FILES", "100001"), ("INDEX_MAX_BYTES", str(513 << 20)), ("MAX_FILE_BYTES", str(9 << 20)),
    ("INDEX_TIMEOUT_SECONDS", "601"), ("KEEP_RAW_CHARS", "4001"), ("CHUNK_CHARS", "10"),
    ("MATCH_LINE_TOLERANCE", "-1"), ("MAX_RECORDS", "0"), ("ALLOW_REMOTE_OLLAMA", "perhaps"),
    ("DATASET", "bad\x00path"), ("REPO_ROOTS", "relative/dir"),
])
def test_a_bad_value_keeps_the_narrow_default_and_reports_an_error(key, value):
    config, errors = parse(**{key: value})
    assert config == SecurityEvalConfig()
    assert len(errors) == 1 and P + key in errors[0]


def test_unrelated_and_blank_keys_are_ignored():
    errors: list[str] = []
    config = security_eval_config_from_env(
        {"SONDER_BUILD_NETWORK": "enforce", P + "MODEL": "  ", "PATH": "x"}, errors)
    assert config == SecurityEvalConfig()
    assert errors == []


def test_sonder_config_carries_the_section_and_load_config_folds_the_environment(tmp_path):
    assert SonderConfig().security_eval == SecurityEvalConfig()
    config = load_config(env={P + "MODEL": "demo:9b", P + "NUM_CTX": "4096", P + "REPO_ROOTS": str(tmp_path)})
    assert config.security_eval.model == "demo:9b" and config.security_eval.num_ctx == 4096
    assert config.security_eval.repo_roots == (str(tmp_path),)
    with pytest.raises(ConfigError):
        load_config(env={P + "NUM_CTX": "12"})
