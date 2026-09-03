"""Runtime update rendering lives in the domain; the root names are delegates."""
import git_tools
import server
from sonder_runtime.domain.updates import runtime_update_formatting as rendering


def _status(**overrides):
    data = {
        "installed_commit": "0123456789abcdef0123", "installed_commit_time": "2026-09-01 10:00",
        "newest_commit": "fedcba9876543210fedc", "newest_commit_time": "2026-09-02 11:00",
        "remote_ref_refreshed": True, "state": "behind", "behind": 3, "ahead": 0,
        "clean": True, "branch": "main", "root": "/srv/sonder", "remote": "origin",
        "trusted_remote": True, "checked_at": "2026-09-03T00:00:00Z",
    }
    data.update(overrides)
    return data


def _verdict(**overrides):
    return rendering.runtime_update_eligibility(_status(**overrides), update_branch="main")


def test_root_delegates_render_through_the_domain_with_the_canonical_branch():
    data = _status()
    assert server._runtime_update_format(data) == rendering.format_runtime_update(
        data, update_branch=git_tools.RUNTIME_UPDATE_BRANCH,
    )
    assert server._runtime_update_eligibility(data) == rendering.runtime_update_eligibility(
        data, update_branch=git_tools.RUNTIME_UPDATE_BRANCH,
    )
    assert server._runtime_update_eligibility(_status(branch="feature")).startswith(
        "refused; checkout must be %r" % git_tools.RUNTIME_UPDATE_BRANCH
    )


def test_the_report_lists_every_field_and_the_update_verdict():
    lines = rendering.format_runtime_update(_status(), update_branch="main").splitlines()
    assert lines == [
        "Sonder source update status:",
        "  installed: 0123456789ab (2026-09-01 10:00)",
        "  newest origin/main: fedcba987654 (2026-09-02 11:00)",
        "  state: behind (behind=3, ahead=0; worktree=clean)",
        "  checkout: main (source root: /srv/sonder)",
        "  remote: origin",
        "  checked: 2026-09-03T00:00:00Z",
        "  update: eligible; /update can fast-forward canonical main",
    ]


def test_running_commit_restart_and_explicit_update_outcomes_are_rendered():
    data = _status(running_commit="abcdef0123456789", restart_required=True)
    lines = rendering.format_runtime_update(data, update_branch="main").splitlines()
    assert lines[2] == "  running: abcdef012345 [restart required]"
    assert "  restart: required; running source differs from the installed checkout" in lines
    assert lines[-1] == "  update: eligible; /update can fast-forward canonical main"
    assert rendering.format_runtime_update(data, updated=True, update_branch="main").endswith(
        "  update: fast-forwarded; restart Sonder to run the new source"
    )
    assert rendering.format_runtime_update(data, updated=False, update_branch="main").endswith(
        "  update: already current; no files changed"
    )
    cached = rendering.format_runtime_update(_status(remote_ref_refreshed=False), update_branch="main")
    assert "  newest known origin/main: fedcba987654 (2026-09-02 11:00)" in cached
    untrusted = rendering.format_runtime_update(
        _status(trusted_remote=False, remote="fork"), update_branch="main",
    )
    assert "  remote: fork [not canonical; update refused]" in untrusted
    assert untrusted.endswith("  update: refused; remote is not the canonical Sonder origin")


def test_eligibility_refuses_in_authority_order_and_names_the_verdict():
    assert _verdict(trusted_remote=False) == "refused; remote is not the canonical Sonder origin"
    assert _verdict(branch="feature") == "refused; checkout must be 'main' (current: 'feature')"
    assert _verdict(branch="") == "refused; checkout must be 'main' (current: 'detached HEAD')"
    assert _verdict(clean=False) == "refused; source checkout is dirty"
    assert _verdict(ahead="many") == "refused; local commit status is unavailable"
    assert _verdict(ahead=2) == "refused; local commits require manual reconciliation"
    assert _verdict(state="current") == "eligible; already current"
    assert _verdict() == "eligible; /update can fast-forward canonical main"
    assert rendering.runtime_update_eligibility(_status(), update_branch="release") == (
        "refused; checkout must be 'release' (current: 'main')"
    )
