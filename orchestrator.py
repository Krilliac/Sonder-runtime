"""Pure learning flow: retrieve -> augment -> generate -> capture."""
import memory_store
import retriever

MEMORY_HEADER = "# Relevant lessons from past work (may help):"


def build_prompt(task, lessons):
    if not lessons:
        return task
    block = "\n".join("- %s" % l for l in lessons)
    return "%s\n%s\n\n# Task:\n%s" % (MEMORY_HEADER, block, task)


def run_with_learning(conn, task, tier, generate_fn,
                      retrieve_fn=retriever.retrieve, id_fn=memory_store.new_id):
    lessons = retrieve_fn(conn, task)
    augmented = build_prompt(task, lessons)
    response = generate_fn(augmented)
    interaction_id = id_fn()
    memory_store.log_interaction(
        conn, interaction_id, task, "\n".join(lessons), response, tier
    )
    return response, interaction_id
