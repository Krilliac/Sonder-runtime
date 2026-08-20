from __future__ import annotations

import pytest

from sonder_runtime.adapters.cli_options import parse_args as packaged_parse_args
from sonder_runtime.bootstrap.main import parse_args as bootstrap_parse_args


def test_bootstrap_parser_is_identity_compatibility_surface():
    assert bootstrap_parse_args is packaged_parse_args


@pytest.mark.parametrize(
    ("argv", "tools", "selfmod", "profile"),
    [
        ([], False, False, "workstation-local"),
        (["--unrestricted-tools"], True, False, "workstation-local"),
        (["--unrestricted-selfmod"], False, True, "workstation-local"),
        (
            ["--unrestricted-tools", "--unrestricted-selfmod", "--profile", "server-private"],
            True,
            True,
            "server-private",
        ),
    ],
)
def test_parse_args_preserves_startup_contract(argv, tools, selfmod, profile):
    args = packaged_parse_args(argv)
    assert args.unrestricted_tools is tools
    assert args.unrestricted_selfmod is selfmod
    assert args.profile == profile


def test_invalid_profile_is_rejected():
    with pytest.raises(SystemExit):
        packaged_parse_args(["--profile", "unknown"])
