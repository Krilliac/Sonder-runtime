"""The claim checker's windows and limits are typed, bounded configuration."""
from dataclasses import replace

import pytest

from sonder_runtime.platform.config import ConfigError, SonderConfig, load_config
from sonder_runtime.platform.claim_check_config import ClaimCheckConfig, claim_check_errors


def test_defaults_are_valid_and_visible_in_diagnostics():
    config = SonderConfig()
    assert config.claim_check.guard_window_lines == 15
    assert claim_check_errors(config) == []
    shown = config.as_redacted_dict()["claim_check"]
    assert shown["max_evidence"] == 20
    assert shown["skip_dirs"] == [".git", "node_modules", "bin", "obj", ".vs", "__pycache__"]


def test_toml_overrides_windows(tmp_path):
    path = tmp_path / "claim.toml"
    path.write_text(
        "[claim_check]\nguard_window_lines = 30\nsymbol_window_lines = 3\n"
        "extra_guard_patterns = ['MyGuard\\(']\n", encoding="utf-8",
    )
    config = load_config(path, env={})
    assert config.claim_check.guard_window_lines == 30
    assert config.claim_check.symbol_window_lines == 3
    assert config.claim_check.extra_guard_patterns == (r"MyGuard\(",)


@pytest.mark.parametrize("field,value", [
    ("guard_window_lines", -1), ("guard_window_lines", 201), ("symbol_window_lines", True),
    ("duplicate_window_lines", 500), ("caller_window_lines", -3),
    ("max_file_bytes", 10), ("max_file_bytes", 10**9), ("max_callers", 101),
    ("max_caller_files", -1), ("max_cache_bytes", 5), ("max_evidence", 0), ("max_batch_claims", 0), ("max_batch_claims", 100001),
    ("skip_dirs", ("a/b",)), ("skip_dirs", (".git", ".git")), ("skip_dirs", ["x"]),
    ("extra_guard_patterns", ("(unclosed",)), ("extra_guard_patterns", ("x" * 300,)),
    ("extra_guard_patterns", tuple("p%d" % i for i in range(40))),
    ("symbol_match", "some"),
])
def test_invalid_values_rejected(field, value):
    config = replace(SonderConfig(), claim_check=replace(ClaimCheckConfig(), **{field: value}))
    assert claim_check_errors(config)


def test_invalid_toml_fails_before_runtime(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[claim_check]\nmax_evidence = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="claim_check"):
        load_config(path, env={})
