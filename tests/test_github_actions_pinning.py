import re
from pathlib import Path

import pytest


WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
USES_LINE = re.compile(
    r"^\s*(?:-\s*)?uses\s*:\s*(?P<reference>[^\s#]+)"
    r"\s*(?P<comment>\#.*)?$"
)
INLINE_USES = re.compile(r"(?:^|[{,]\s*)uses\s*:")
REMOTE_ACTION = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"(?:/[A-Za-z0-9_.\-/]+)?@[0-9a-fA-F]{40}$"
)
DOCKER_ACTION = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-fA-F]{64}$")
LOCAL_ACTION = re.compile(
    r"^\./(?!\.\.(?:/|$))(?!.*(?:/)\.\.(?:/|$))[A-Za-z0-9_.\-/]+$"
)
VERSION_COMMENT = re.compile(r"^#\s+v\d+(?:\b|[._-])")


def _workflow_files(root=WORKFLOWS_DIR):
    return sorted((*root.glob("*.yml"), *root.glob("*.yaml")))


def _uses_entries(path):
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = USES_LINE.match(line)
        if match:
            reference = match.group("reference").strip("'\"")
            comment = (match.group("comment") or "").strip()
            yield line_number, reference, comment
        elif INLINE_USES.search(line) and not line.lstrip().startswith("#"):
            yield line_number, "<non-canonical uses declaration>", ""


def _is_immutable_reference(reference):
    return bool(
        LOCAL_ACTION.fullmatch(reference)
        or DOCKER_ACTION.fullmatch(reference)
        or REMOTE_ACTION.fullmatch(reference)
    )


def test_all_workflow_actions_use_immutable_references():
    violations = []
    for workflow in _workflow_files():
        for line_number, reference, _ in _uses_entries(workflow):
            if not _is_immutable_reference(reference):
                violations.append(f"{workflow.name}:{line_number}: {reference}")

    assert not violations, "Unpinned GitHub Action references:\n" + "\n".join(violations)


def test_remote_action_pins_keep_human_readable_version_comments():
    violations = []
    for workflow in _workflow_files():
        for line_number, reference, comment in _uses_entries(workflow):
            if REMOTE_ACTION.fullmatch(reference) and not VERSION_COMMENT.match(comment):
                violations.append(f"{workflow.name}:{line_number}: {reference}")

    assert not violations, "Remote action pins without version comments:\n" + "\n".join(
        violations
    )


@pytest.mark.parametrize(
    "reference",
    [
        "./.github/actions/build",
        "docker://ghcr.io/example/action@sha256:" + "a" * 64,
        "owner/action@" + "a" * 40,
        "owner/action/subdirectory@" + "A" * 40,
    ],
)
def test_immutable_reference_policy_allows_supported_pins(reference):
    assert _is_immutable_reference(reference)


@pytest.mark.parametrize(
    "reference",
    [
        "actions/checkout@v7",
        "actions/checkout@main",
        "actions/checkout@deadbeef",
        "docker://alpine:3.22",
        "docker://alpine@sha256:deadbeef",
        "./../outside-repository",
        "${{ matrix.action }}",
    ],
)
def test_immutable_reference_policy_rejects_movable_or_ambiguous_refs(reference):
    assert not _is_immutable_reference(reference)
