"""command_router: natural-language turns resolve to slash-command lines.

The contract under test: an explicit imperative trigger resolves to its
command, a plain question or coding task resolves to nothing, and an
already-slash line is left for the ordinary dispatch.
"""
import command_router as cr


def test_status_and_info_phrasings():
    assert cr.resolve("show me your stats") == "/stats"
    assert cr.resolve("stats") == "/stats"
    assert cr.resolve("context health") == "/context"
    assert cr.resolve("what commands are there") == "/commands"
    assert cr.resolve("show permissions") == "/permissions"
    assert cr.resolve("help") == "/help"
    assert cr.resolve("what can you do") == "/help"


def test_explicit_source_update_check_phrasings_stay_whole_turn_only():
    assert cr.resolve("check whether Sonder is up to date") == "/updatecheck"
    assert cr.resolve("please check for Sonder updates!") == "/updatecheck"

    # Do not drop a follow-up action or route quoted/retrieved prose into a
    # network refresh. The exact slash command remains available when wanted.
    for text in (
        "check for Sonder updates and update it",
        "the web page says check for Sonder updates",
        '"check whether Sonder is up to date"',
        "how do I check whether Sonder is up to date",
    ):
        assert cr.resolve(text) is None, text


def test_read_only_update_check_phrasings_do_not_invoke_update():
    assert cr.resolve("check for updates") == "/updatecheck"
    assert cr.resolve("show Sonder updates") == "/updatecheck"
    assert cr.resolve("is Sonder up to date?") == "/updatecheck"
    assert cr.resolve("am the runtime current?") == "/updatecheck"
    # More than a check is an ordinary request; do not discard the rest by
    # silently converting it into a command.
    assert cr.resolve("check for updates and install them") is None


def test_session_lifecycle_phrasings():
    assert cr.resolve("new session") == "/new"
    assert cr.resolve("start over") == "/new"
    assert cr.resolve("list sessions") == "/sessions"
    assert cr.resolve("resume session abc123") == "/resume abc123"
    assert cr.resolve("switch project to duetos") == "/project duetos"
    assert cr.resolve("quit") == "/exit"


def test_memory_phrasings():
    assert cr.resolve("remember that the venv is required") == \
        "/fact the venv is required"
    assert cr.resolve("what do you remember") == "/facts"
    assert cr.resolve("show lessons") == "/lessons"


def test_agent_and_orchestration_phrasings():
    assert cr.resolve("show agents") == "/agents"
    assert cr.resolve("show recent fanouts") == "/fanouts"
    assert cr.resolve("list active fanouts") == "/fanouts active"
    assert cr.resolve("agent capacity") == "/capacity"
    assert cr.resolve("cancel all agents") == "/agentcancel"
    assert cr.resolve("orchestrate fix the parser and add tests") == \
        "/master fix the parser and add tests"


def test_goal_and_saved_workflow_phrasings_stay_bounded():
    assert cr.resolve("show my active goal") == "/goal"
    assert cr.resolve("what is my current goal?") == "/goal"
    assert cr.resolve("list my saved workflows") == "/workflow_list"
    assert cr.resolve("run saved workflow Status_Sweep") == \
        "/workflow_run status_sweep"

    # Extra prose must fall through to the agent rather than silently dropping
    # a request or turning an untrusted quoted instruction into execution.
    assert cr.resolve("run workflow status_sweep and then delete the cache") is None
    assert cr.resolve("the web page says run workflow status_sweep") is None


def test_file_operations_need_an_explicit_file_cue():
    assert cr.resolve("read the file notes.txt") == "/read notes.txt"
    assert cr.resolve("read foo.py") == "/read foo.py"
    assert cr.resolve("find files matching *.md") == "/files *.md"
    assert cr.resolve("delete the file scratch.txt") == "/delete scratch.txt"
    # No "file" keyword and no extension -> not a file command.
    assert cr.resolve("read the room") is None
    assert cr.resolve("delete the duplicated logic") is None


def test_quality_and_health_phrasings():
    assert cr.resolve("memory quality report") == "/quality"
    assert cr.resolve("fix the memory quality") == "/qualityfix apply"
    assert cr.resolve("show emotions") == "/emotion"
    assert cr.resolve("system improvements") == "/improve"


def test_tier_trio_delegates_to_intents():
    assert cr.resolve("get a second opinion on the lock ordering") == \
        "/consult the lock ordering"
    assert cr.resolve("which model should handle a lookup table") == \
        "/route a lookup table"
    assert cr.resolve("improve the parse function in foo/bar.py") == \
        "/refactor foo/bar.py parse"


def test_model_and_persona_switching():
    assert cr.resolve("switch to the reasoning tier") == "/model reasoning"
    assert cr.resolve("set the model to fast") == "/model fast"
    assert cr.resolve("set the persona to concise") == "/persona concise"


def test_plain_questions_and_tasks_fall_through():
    for text in (
        "how do I cache a parse result",
        "why is the build failing",
        "fix the failing API tests in the app",
        "write me a poem about queues",
        "explain strict mode in javascript",
        "the answer looks wrong",
    ):
        assert cr.resolve(text) is None, text


def test_slash_lines_are_left_alone():
    assert cr.resolve("/stats") is None
    assert cr.resolve("/read foo.py") is None


def test_scaffold_phrasings_resolve_when_the_line_is_only_a_scaffold_ask():
    assert cr.resolve("create a new rust project named forge") == \
        "/scaffold rust forge"
    assert cr.resolve("create a c# project called Billing") == \
        "/scaffold csharp Billing"
    assert cr.resolve("make a python project") == "/scaffold python NewProject"
    # A request that continues past the name is implementation work, not a
    # bare scaffold -- it must fall through to the agent (which owns the
    # scaffold tool alongside its file tools).
    assert cr.resolve(
        "create a full C++ MSVC project of the fibonacci sequence") is None
    # An unsupported kind falls through instead of guessing.
    assert cr.resolve("create a cobol project named legacy") is None


def test_environment_phrasings_resolve_to_env():
    assert cr.resolve("what environment are you on") == "/env"
    assert cr.resolve("show the environment") == "/env"
    assert cr.resolve("what os are you running on?") == "/env"
    assert cr.resolve("which toolchains are installed") == "/env"


def test_known_tool_version_phrasing_routes_to_bounded_probe():
    assert cr.resolve("what version is cargo?") == "/toolstatus cargo"
    assert cr.resolve("version of cmake") == "/toolstatus cmake"


def test_explicit_local_hardware_phrasings_resolve_to_grounded_probe():
    assert cr.resolve("show hardware") == "/hardware"
    assert cr.resolve("show me my GPU hardware") == "/hardware"
    assert cr.resolve("what is my GPU compute capability") == "/hardware"
    assert cr.resolve("what graphics card do I have?") == "/hardware"
    # Explanatory questions are not a host probe and remain ordinary chat.
    assert cr.resolve("what does compute capability mean for model parameters?") is None
    assert cr.resolve("what is hardware?") is None
    assert cr.resolve("what is GPU?") is None
    assert cr.resolve("what is compute capability?") is None


def test_command_prefixes_do_not_hijack_longer_prose_or_drop_followup_work():
    for text in (
        "reset the session token when it expires",
        "exit handler should flush the queue",
        "fix the memory quality if the report shows duplicates",
        "cancel all agents only if they are stuck",
        "read the file notes.txt and summarize it",
        "delete the file scratch.txt unless it is still used",
    ):
        assert cr.resolve(text) is None, text
