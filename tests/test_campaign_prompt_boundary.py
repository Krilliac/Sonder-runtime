"""Campaign prompts live in the domain; the root name is a delegate."""
import server
from sonder_runtime.adapters.execution_tools import grounding
from sonder_runtime.domain import campaign_prompt as prompts


def test_root_delegate_injects_the_grounding_fences():
    assert server._campaign_prompt("python", "list", "sum the list") == prompts.campaign_prompt(
        "python", "list", "sum the list", fences=grounding._LANG_FENCE,
    )


def test_prompt_names_the_language_task_fence_and_repair_note():
    text = prompts.campaign_prompt("python", "list", "sum the list", fences={"python": "py"})
    assert text.startswith("Write a complete runnable python program for this task: sum the list.\n")
    assert "Return only one ```py code block." in text
    assert text.endswith("The program must terminate quickly.")
    repaired = prompts.campaign_prompt("go", "list", "sum", "boom", fences={})
    assert "Return only one ```go code block." in repaired
    assert repaired.endswith("The program must terminate quickly.\nPrevious attempt failed:\nboom\nFix it.")


def test_language_notes_apply_only_to_their_task():
    assert "-join" in prompts.campaign_prompt("powershell", "string", "reverse", fences={})
    assert "Measure-Object" in prompts.campaign_prompt("powershell", "list", "sum", fences={})
    assert "<algorithm>" in prompts.campaign_prompt("cpp", "string", "reverse", fences={})
    assert "<algorithm>" not in prompts.campaign_prompt("cpp", "list", "sum", fences={})
