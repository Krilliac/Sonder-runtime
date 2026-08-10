"""command_router: the generic ``command_catalog`` fallback.

``test_command_router.py`` covers the hand-written rules. This file covers the
fallback that runs after they all miss, and the property that matters most
about it: the whole 265-command surface became reachable in plain language
*without* ordinary prose or a real coding question becoming a command.

The four contracts under test:

* the hand-written rules keep first refusal, because they extract arguments
  the generic path cannot ("read the file notes.txt" -> ``/read notes.txt``,
  not ``/file_read``);
* an imperative opening or an outright command name resolves generically;
* prose, questions, and work requests resolve to nothing;
* mutation and dangerous commands are only reachable when named.
"""
import pytest

import command_catalog
import command_router as cr


# --- 1. the hand-written rules still win ----------------------------------


def test_hand_written_rules_keep_precedence_over_the_catalog():
    """Each of these also matches a catalog command; the rule must win.

    The rules encode argument extraction and curated targets that a generic
    name match cannot reproduce -- /read over /file_read, /context over
    /context_health, /delete (which shows the confirmation token) over
    /file_delete.
    """
    assert cr.resolve("read the file notes.txt") == "/read notes.txt"
    assert cr.resolve("delete the file scratch.txt") == "/delete scratch.txt"
    assert cr.resolve("find files matching *.md") == "/files *.md"
    assert cr.resolve("context health") == "/context"
    assert cr.resolve("list sessions") == "/sessions"
    assert cr.resolve("show me your stats") == "/stats"
    assert cr.resolve("cancel all agents") == "/agentcancel"
    assert cr.resolve("show lessons") == "/lessons"
    assert cr.resolve("orchestrate fix the parser") == "/master fix the parser"


def test_tier_trio_still_precedes_the_catalog():
    assert cr.resolve("get a second opinion on the lock ordering") == \
        "/consult the lock ordering"
    assert cr.resolve("which model should handle a lookup table") == \
        "/route a lookup table"


# --- 2. the generic catalog match -----------------------------------------


def test_generic_matches_on_command_names():
    assert cr.resolve("scan for secrets") == "/secret_scan"
    assert cr.resolve("run the tests") == "/test_run"
    assert cr.resolve("what's my task progress") == "/task_progress"
    assert cr.resolve("format the code") == "/format_code"
    assert cr.resolve("list processes") == "/process_list"
    assert cr.resolve("check the environment status") == "/environment_status"
    assert cr.resolve("inspect the hardware profile") == "/hardware_profile"
    assert cr.resolve("export the session") == "/session_export"
    assert cr.resolve("compare the workspaces") == "/workspace_compare"
    assert cr.resolve("show npu status") == "/npu_status"


def test_generic_matches_survive_plurals_and_politeness():
    """"processes"/"secrets" are the same word as the command's, and a
    request wrapper is not content."""
    assert cr.resolve("please scan for secrets") == "/secret_scan"
    assert cr.resolve("can you list the workflows") == "/workflow_list"
    assert cr.resolve("i want to delete task abc") == "/task_delete abc"
    assert cr.resolve("list processes") == cr.resolve("show the process list")


def test_a_named_command_needs_no_imperative_opening():
    """"what's my task progress" is a question, but it names the command."""
    assert cr.resolve("what is the task progress") == "/task_progress"
    assert cr.resolve("git commit") == "/git_commit"
    assert cr.resolve("apply patch") == "/apply_patch"


def test_summary_words_stand_in_for_part_of_a_name():
    """Half the name plus distinctive summary words is enough.

    /secret_scan's summary is "Scan workspace files for leaked secrets (API
    keys, passwords, private keys, tokens)" -- "leaked", "api" and "keys" are
    rare enough across the catalog to identify it.
    """
    assert cr.resolve("scan for leaked api keys") == "/secret_scan"
    assert cr.resolve("scan the workspace for secrets") == "/secret_scan"


def test_a_trailing_word_becomes_the_argument_only_when_one_is_required():
    # /task_delete requires task_id, so the one leftover word is that id.
    assert cr.resolve("delete task abc") == "/task_delete abc"
    assert cr.resolve("delete the task abc") == "/task_delete abc"
    # /test_run requires nothing, so "suite" is unexplained content, not an
    # argument, and the turn is left for the agent.
    assert cr.resolve("run the test suite") is None


def test_every_generic_result_is_a_real_catalog_command():
    for text in (
        "scan for secrets", "run the tests", "what's my task progress",
        "format the code", "list processes", "export the session",
        "delete task abc", "git commit", "check the context health",
    ):
        line = cr.resolve(text)
        assert line, text
        assert command_catalog.by_name(line.split()[0]) is not None, text


def test_most_multi_word_commands_are_reachable_by_name():
    """Breadth check: the point of the fallback is coverage, not a few cases.

    Counted over the safe/ask commands whose name is more than one word, so a
    catalog that failed to load (making this vacuously "clean") fails the
    population assertion first.
    """
    population = [
        c for c in command_catalog.catalog()
        if len(cr._tokenize(c.name)) >= 2 and c.risk not in ("mutation", "dangerous")
    ]
    assert len(population) > 100, "catalog did not load; the rest is vacuous"
    reached = [
        c for c in population
        if (cr.resolve("show the " + c.name.lstrip("/").replace("_", " ")) or "")
        .split(" ")[0] == c.name
    ]
    assert len(reached) >= 0.8 * len(population), (
        "only %d of %d multi-word commands were reachable" % (
            len(reached), len(population))
    )


# --- 3. prose and real work must fall through -----------------------------


def test_coding_questions_and_work_requests_resolve_to_nothing():
    for text in (
        "how do I cache a parse result",
        "fix the failing API tests",
        "the build is broken, any idea why",
        "what does this function do",
        "reset the session token when it expires",
        "write a haiku about files",
    ):
        assert cr.resolve(text) is None, text


def test_more_prose_that_must_not_become_a_command():
    for text in (
        "can you fix the failing tests",
        "please clean up the repo",
        "explain how the task progress bar is computed",
        "the file policy keeps rejecting my writes",
        "why did the format step change every line",
        "i think the secret scan missed the .env file",
        "should I run the tests before or after the refactor",
    ):
        assert cr.resolve(text) is None, text


def test_a_single_shared_word_never_elects_a_command():
    """One word overlapping a name or summary is not a match."""
    assert cr.resolve("read the room") is None
    assert cr.resolve("create a file") is None
    assert cr.resolve("check status") is None
    assert cr.resolve("look at the tasks") is None
    assert cr.resolve("delete the duplicated logic") is None


def test_leftover_words_mean_the_turn_asks_for_more_than_the_command():
    for text in (
        "read the file notes.txt and summarize it",
        "format the code in the payment module",
        "delete the file scratch.txt unless it is still used",
        "fix the memory quality if the report shows duplicates",
        "cancel all agents only if they are stuck",
    ):
        assert cr.resolve(text) is None, text


def test_slash_lines_and_blanks_are_left_alone():
    assert cr.resolve("/secret_scan") is None
    assert cr.resolve("") is None
    assert cr.resolve(None) is None
    assert cr.resolve("   ") is None


# --- 4. mutation / dangerous gating ---------------------------------------


def test_risky_commands_need_their_name_adjacent_in_the_turn():
    # Named outright -> allowed.
    assert cr.resolve("delete task abc") == "/task_delete abc"
    assert cr.resolve("git commit") == "/git_commit"
    # The same words scattered across the turn are not a name.
    assert cr.resolve("delete all of the tasks") is None
    assert cr.resolve("commit the changes") is None


def test_risky_commands_are_never_reached_from_a_loose_match():
    for text in (
        "clean up",
        "clean the build artifacts",
        "search for a symbol across files matching a glob",
        "install the packages",
        "stash the changes",
    ):
        assert cr.resolve(text) is None, text


def test_no_mutating_command_is_reachable_from_its_summary_alone():
    """Property over the whole catalog, not a sample.

    For every mutation/dangerous command, a turn built from its own summary
    words -- minus any word of its name -- must not reach it. This is the
    rule that keeps "clean up"-shaped asks away from destructive tools even as
    new tools are added.
    """
    risky = [c for c in command_catalog.catalog()
             if c.risk in ("mutation", "dangerous")]
    assert len(risky) > 20, "catalog did not load; the rest is vacuous"
    for command in risky:
        named = set()
        for name in command.all_names:
            named.update(cr._tokenize(name))
        words = [t for t in cr._tokenize(command.summary)
                 if t not in cr._STOPWORDS and t not in named][:6]
        if not words:
            continue
        phrase = "show " + " ".join(words)
        line = cr.resolve(phrase)
        assert not (line and line.split()[0] == command.name), \
            "%s reachable from its summary: %r" % (command.name, phrase)


# --- 5. ambiguity ---------------------------------------------------------


def test_a_tie_resolves_to_nothing():
    """"grep files" matches /search (alias /grep) and /files equally well.

    Guessing between them is exactly the failure the fallback must not have.
    """
    assert cr.resolve("grep files") is None


def test_ambiguity_is_decided_before_the_turn_is_accepted():
    ranked = _rank("grep files")
    assert len(ranked) >= 2
    assert ranked[0][0] == ranked[1][0], "expected a genuine tie"
    assert cr.resolve("grep files") is None
    # One more distinguishing word breaks the tie and the turn resolves.
    assert cr.resolve("search the files") == "/search"


def _rank(text):
    """The (score, name) list the resolver ranks, for tie assertions."""
    entries = cr._index()
    tokens = cr._tokenize(text)
    _lead, body = cr._opening(tokens)
    content = {t for t in body if t not in cr._STOPWORDS}
    ranked = []
    for entry in entries:
        scored = cr._score(entry, set(body), content)
        if scored:
            ranked.append(((scored[0], scored[1]), entry["command"].name))
    ranked.sort(key=lambda row: row[0], reverse=True)
    return ranked


# --- 6. index caching -----------------------------------------------------


def test_the_index_follows_the_catalog_across_a_reset():
    """A live reload replaces the catalog tuple; the index must rebuild."""
    assert cr.resolve("scan for secrets") == "/secret_scan"
    command_catalog.reset_cache()
    assert cr.resolve("scan for secrets") == "/secret_scan"
    assert cr._index()


# --- 7. a read verb never reaches a mutating command ----------------------


@pytest.mark.parametrize(
    "turn",
    [
        "list the git branches",
        "show me the git branches",
        "show the git tags",
        "view the git stash",
    ],
)
def test_asking_to_be_shown_something_never_runs_a_mutation(turn):
    """"list the git branches" names /git_branch, which CREATES a branch.

    Naming a command normally suffices even when it mutates, but a read verb
    contradicts the name: the turn is asking to look, not to change. Routing
    it would put a create/modify prompt in front of someone who asked a
    question.
    """
    assert cr.resolve(turn) is None


@pytest.mark.parametrize(
    "turn,expected",
    [
        ("git commit", "/git_commit"),
        ("git stash", "/git_stash"),
        ("format the code", "/format_code"),
        ("delete task abc", "/task_delete abc"),
    ],
)
def test_the_read_verb_gate_does_not_disarm_named_mutations(turn, expected):
    """The gate keys on the opening verb, so an explicit ask still lands."""
    assert cr.resolve(turn) == expected


def test_a_read_verb_still_reaches_a_read_only_command():
    assert cr.resolve("show the task progress") == "/task_progress"
