"""Pure learning flow: retrieve -> augment -> generate -> capture.

The current turn is augmented with facts (durable), lessons (distilled tips), and
recalls (similar past solutions), then answered. `history` (prior conversation turns)
is passed to the model as real chat messages, not folded into the prompt text.
"""
import memory_store
import retriever

MEMORY_HEADER = "# Relevant lessons from past work (may help):"
RECALL_HEADER = "# Similar things solved before (for reference):"
FACTS_HEADER = "# Project facts (always true here):"
RUN_COMPAT_HEADER = "# /run compatibility requirements:"


APPLICATION_HEADER = "# How to apply the lessons:"


def _needs_run_compatible_code(task):
    text = (task or "").lower()
    if "/run" in text:
        return True
    language_request = any(
        word in text
        for word in (
            "python", "pygame", "javascript", "node", "powershell",
            "c++", "cpp", "c plus plus", "csharp", "c#", "c sharp",
        )
    )
    game_request = "game" in text and language_request
    run_request = any(
        phrase in text
        for phrase in (
            "tell failure",
            "until failure",
            "tell me when",
            "run them",
            "run it",
            "will run",
            "increasing complexity",
            "increasing difficulty",
        )
    )
    return game_request and run_request


def _requested_run_language(task):
    text = (task or "").lower()
    if any(word in text for word in ("c++", "cpp", "c plus plus")):
        return "cpp", "C++"
    if any(word in text for word in ("csharp", "c#", "c sharp")):
        return "csharp", "C#"
    if any(word in text for word in ("javascript", "node", "js")):
        return "javascript", "JavaScript"
    if any(word in text for word in ("powershell", "pwsh", "ps1")):
        return "powershell", "PowerShell"
    return "python", "Python"


def _run_compat_block(task):
    fence, title = _requested_run_language(task)
    return (
        "%s\n"
        "- Return exactly one fenced ```%s code block containing complete runnable %s source.\n"
        "- The code must complete under `/run` without keyboard input or external packages.\n"
        "- Do not include `/run ...`, `python file.py`, `pip ...`, or other shell commands in code fences.\n"
        "- For games and demos, include a scripted smoke-test/demo mode that simulates moves or frames, prints PASS/FAIL details, and exits.\n"
        "- Avoid unbounded while/event loops unless they have an auto-exit path that runs by default.\n"
        "- If the user wants a separate console window, generate the normal runnable code and tell them to use `/runwindow` after it."
        % (RUN_COMPAT_HEADER, fence, title)
    )


def build_prompt(task, lessons, recalls=None, facts=None):
    blocks = []
    if _needs_run_compatible_code(task):
        blocks.append(_run_compat_block(task))
    if facts:
        blocks.append("%s\n%s" % (FACTS_HEADER, "\n".join("- %s" % f for f in facts)))
    if lessons:
        blocks.append("%s\n%s" % (MEMORY_HEADER, "\n".join("- %s" % l for l in lessons)))
        blocks.append(
            "%s\nUse the relevant lessons above as constraints while solving. "
            "Prefer lessons with concrete APIs, algorithms, or pitfalls that match this task." %
            APPLICATION_HEADER
        )
    if recalls:
        blocks.append("%s\n%s" % (RECALL_HEADER, "\n".join("- %s" % r for r in recalls)))
    if not blocks:
        return task
    return "%s\n\n# Task:\n%s" % ("\n\n".join(blocks), task)


def _run(conn, task, tier, generate_fn, retrieve_fn=retriever.retrieve,
         id_fn=memory_store.new_id, history=None, recalls=None, facts=None,
         session_id=None, task_embedding=None):
    lesson_rows = None
    if retrieve_fn is retriever.retrieve:
        lesson_rows = retriever.retrieve_with_ids(conn, task)
        lessons = [r["text"] for r in lesson_rows]
    else:
        lessons = retrieve_fn(conn, task)
    augmented = build_prompt(task, lessons, recalls, facts)
    # Existing callers/tests pass a 1-arg gen; only pass history when present.
    response = generate_fn(augmented, history) if history else generate_fn(augmented)
    interaction_id = id_fn()
    memory_store.log_interaction(
        conn, interaction_id, task, "\n".join(lessons), response, tier,
        session_id=session_id, task_embedding=task_embedding,
    )
    if lesson_rows:
        memory_store.log_lesson_usage(
            conn, [r["id"] for r in lesson_rows], interaction_id, task)
    return response, interaction_id, lessons, augmented


def run_with_learning(conn, task, tier, generate_fn,
                      retrieve_fn=retriever.retrieve, id_fn=memory_store.new_id,
                      history=None, recalls=None, facts=None,
                      session_id=None, task_embedding=None):
    response, interaction_id, _lessons, _augmented = _run(
        conn, task, tier, generate_fn, retrieve_fn, id_fn,
        history=history, recalls=recalls, facts=facts,
        session_id=session_id, task_embedding=task_embedding,
    )
    return response, interaction_id


def run_with_learning_traced(conn, task, tier, generate_fn,
                             retrieve_fn=retriever.retrieve, id_fn=memory_store.new_id,
                             history=None, recalls=None, facts=None,
                             session_id=None, task_embedding=None):
    response, interaction_id, lessons, augmented = _run(
        conn, task, tier, generate_fn, retrieve_fn, id_fn,
        history=history, recalls=recalls, facts=facts,
        session_id=session_id, task_embedding=task_embedding,
    )
    return response, interaction_id, {"lessons": lessons, "augmented_prompt": augmented}
