"""Keep the read-only token scope on both CI workflow entry points."""

import re
from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_ci_workflows_limit_token_to_repository_read():
    for name in ("ci.yml", "codegen-container-qualification.yml"):
        workflow = (_WORKFLOWS / name).read_text(encoding="utf-8")
        header, separator, _jobs = workflow.partition("\njobs:\n")

        assert separator, f"{name} has no top-level jobs mapping"
        assert re.findall(r"(?m)^permissions:\n(?:  [^\n]+\n)+", header) == [
            "permissions:\n  contents: read\n"
        ], name
