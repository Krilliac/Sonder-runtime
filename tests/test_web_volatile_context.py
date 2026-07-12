"""Conversation-aware routing for facts whose answers change over time."""
import time

import pytest

import server
import web_intents
import web_tools


@pytest.mark.parametrize(("prompt", "query"), [
    ("Who is OpenAI's CEO?", "Who is the current CEO of OpenAI?"),
    ("Who is the president of France?", "Who is the current president of France?"),
    ("president of France", "Who is the current president of France?"),
    ("What is the price of Bitcoin?", "What is the current price of Bitcoin?"),
    ("Apple stock price?", "What is the current stock price of Apple?"),
    ("What's Apple's stock price?", "What is the current stock price of Apple?"),
    ("USD/EUR exchange rate?", "What is the current USD/EUR exchange rate?"),
    ("When is the next F1 race?", "When is the next F1 race?"),
    ("What's the next Lakers game?", "When is the next Lakers game?"),
    ("What are the NBA standings?", "What are the current NBA standings?"),
    (
        "Who is the current commissioner of the Internal Revenue Service?",
        "Who is the current commissioner of the Internal Revenue Service?",
    ),
    ("What is the latest .NET version?", "What is the latest .NET version?"),
    ("When is the next AC/DC concert?", "When is the next AC/DC concert?"),
    (
        "What are the current 2025/26 NBA standings?",
        "What are the current 2025/26 NBA standings?",
    ),
    (
        "What is the latest ASP.NET/Core version?",
        "What is the latest ASP.NET/Core version?",
    ),
    (
        "Explain why the current CEO of OpenAI resigned",
        "Explain why the current CEO of OpenAI resigned",
    ),
    (
        "How did Bitcoin price change today?",
        "How did Bitcoin price change today?",
    ),
    (
        "What does the current CEO plan to do?",
        "What does the current CEO plan to do?",
    ),
    (
        "What is today's Bitcoin price compared with 2020?",
        "What is today's Bitcoin price compared with 2020?",
    ),
    (
        "What is the current Apple stock price versus the previous close?",
        "What is the current Apple stock price versus the previous close?",
    ),
    (
        "What does the current CEO say about the 2020 election?",
        "What does the current CEO say about the 2020 election?",
    ),
    (
        "What are the latest NBA standings compared with the previous season?",
        "What are the latest NBA standings compared with the previous season?",
    ),
])
def test_implicit_volatile_facts_become_standalone_current_queries(prompt, query):
    assert web_intents.classify(prompt) == {"kind": "research", "query": query}


@pytest.mark.parametrize("prompt", [
    "Who was president of France in 1995?",
    "Who was Apple's CEO in 2011?",
    "Bitcoin price on Jan 1 2020?",
    "What does a CEO do?",
    "Which president signed the ACA?",
    "Explain price elasticity",
    "How does round-robin scheduling work?",
    "What Python version supports match statements?",
    "Who is the president?",
    "What's the price?",
    "When is my next calendar meeting?",
    "When does the next cron job run?",
    "Who is president of our chess club?",
    "What Python version do I have?",
    "What version does this repo use?",
    "What is the price field in this JSON?",
    "What is the current node in this linked list?",
    "What is an F1 race condition in threading?",
    "Who is the manager of the src directory?",
    "Who is the president of the chess club?",
    "What is the next pytest fixture?",
    "When is the next regex match?",
    "What is the current version in our private repo Sonder?",
    'Is the phrase "current CEO" grammatically correct?',
    "What is the price of freedom?",
    "What is unit price?",
    "What is shadow price?",
    "How much is a human life worth?",
    "When is the next team meeting?",
    "When is the next board meeting?",
    "Who is the current CEO of my startup?",
    "Who is the current president of our neighborhood association?",
    "What is the current price of my used car?",
    "What is the current version of my internal tool?",
    "What does the current CEO of my startup plan to do?",
    "What is the current version in C:\\Clients\\AcmeSecret\\app.config?",
    "What is the current version in our package.json?",
    "What is the latest dependency version in our requirements.txt?",
    "What is the current release in our internal app?",
    "Who is our current president?",
    "Who is our current CEO?",
    "What is the latest API version in our service?",
    "What is the current version in appsettings.json?",
    "What is the current version in go.mod?",
    "What is the current version in .\\src\\config.yaml?",
    "What is the current version in ..\\secret\\version.txt?",
    "What is the current version in ./private/version.txt?",
    "What is the current version in \\\\server\\share\\version.txt?",
    "What is the current version in $HOME/private/version.txt?",
    "What is the current version in %USERPROFILE%\\private\\version.txt?",
    "What is the current version in our config.toml?",
    "What is the current version in our pom.xml?",
    "What is the current version in our .env?",
    "What is the current version in src/version.txt?",
    "What is the current version in config/version.txt?",
    "What is the current version in docs/VERSION?",
    "What is the current version in Gemfile?",
    "What is the current version from build.gradle?",
    "What is the current version in BUILD.bazel?",
    "What is the current version in Jenkinsfile?",
    "What is the current version in this project?",
    "What is the latest version in the project?",
    "What is the current version in this code?",
    "What is the current price in this JSON?",
    "What is the latest API version in this app?",
    "Which current version is configured here?",
    "What is the current version according to config.toml?",
    "What is the current version recorded by VERSION.txt?",
])
def test_historical_static_private_and_code_shapes_stay_offline(prompt):
    assert web_intents.classify(prompt) is None


@pytest.mark.parametrize("prompt", [
    'Translate "Who is the current CEO of OpenAI?" into Spanish',
    'Explain why "Who is the current president?" is ambiguous',
    "Explain why 'Who is the current CEO of OpenAI?' is ambiguous",
    "Explain why the phrase current CEO is ambiguous",
    "Do not look up the current Apple stock price",
    "I am not asking for the current CEO; explain the role",
    "Assume Alice is the current CEO of Acme. Who reports to her?",
    "Does this regex match current CEO questions?",
    "How does Python decide its latest version?",
    "Write code to fetch the current Bitcoin price.",
    "Update Node.js to the latest version in this repo",
    "Add a stock-price widget to the app",
    "Install the latest version of Python",
    "Use the latest Python version for this project",
    "Download the latest version of Node.js",
    "Upgrade to the current Node.js release",
    "Use the current browser version in this project",
    "Use the latest web API version in this project",
    "Use the latest online API version in this project",
    "Explain why 'current CEO' is ambiguous",
    "Analyze the term 'current price'",
    "Discuss 'latest version' semantics",
    "Explain 'current president' in this sentence",
    "An app should show current Bitcoin price",
    "This website should display current Apple stock price",
    "The page must show latest Python version",
    "This UI will display current NBA standings",
    "The README should mention latest Node.js",
    "The report needs to include current CEO",
])
def test_meta_negated_hypothetical_and_work_frames_do_not_route(prompt):
    assert web_intents.classify(prompt) is None


@pytest.mark.parametrize("prompt", [
    "Who plays President Snow?",
    "Who is Jon Snow?",
    "What is Snow Crash about?",
    "What is Snow crash about?",
    "Explain snow crash",
    "Who is Rain?",
    "What is Purple Rain about?",
    "What is Raining Blood about?",
    "What about Snow Crash?",
    "How about Snow White?",
    "What about Rain Man?",
    "How much Snow Crash sold?",
    "Who sings It's Raining Men?",
    "What is It's Raining Men about?",
    "Raining in My Heart song meaning",
    "Is It Raining in Paris a movie?",
    "What does 'it is raining' mean?",
])
def test_snow_surnames_are_not_weather(prompt):
    assert web_intents.classify(prompt) is None


def test_how_can_i_use_web_remains_a_capability_question():
    assert web_intents.classify("How can I use the web?") == {"kind": "capability"}


@pytest.mark.parametrize("prompt", [
    "Will it rain tomorrow?",
    "Is it snowing in Denver?",
    "Is there any snow in Madison?",
    "Chance of rain in Chicago?",
    "Snow forecast for Boston",
    "What about rain in Chicago tomorrow?",
    "How much rain will Chicago get tomorrow?",
    "What about snow in Denver?",
    "How much rain fell in Chicago today?",
    "What about snow accumulation in Denver?",
    "How much snowfall in Denver?",
])
def test_precipitation_with_weather_syntax_still_routes(prompt):
    assert web_intents.classify(prompt)["kind"] == "weather"


def test_precipitation_amount_extracts_location():
    assert web_intents.classify("How much rain will Chicago get tomorrow?") == {
        "kind": "weather", "location": "Chicago",
    }


@pytest.mark.parametrize("prompt", [
    "Use the web to tell me current news",
    "Use the browser to find today's Python release notes",
    "Use the web to tell me who wrote Hamlet",
    "Use the internet to answer who wrote Hamlet",
    "Use the web to get the OpenAI homepage URL",
    "Please use the web to tell me who wrote Hamlet",
    "Search the web for how to build a PC",
    "Search the web for tutorials to install Python",
    "Look it up online to learn how to configure CMake",
    "Use the web to learn how to build a deck",
    "Web search for ways to fix a leaking faucet",
    "Search the web for a guide to write a resume",
    "Can you use the web to tell me who wrote Hamlet",
    "Could you use the internet to answer who wrote Hamlet",
    "Would you use the browser to verify the OpenAI homepage",
    "Will you use web tools to find the Node.js homepage",
])
def test_explicit_use_web_action_is_not_mistaken_for_workspace_work(prompt):
    assert web_intents.classify(prompt)["kind"] == "research"


@pytest.mark.parametrize(("previous", "followup", "query"), [
    (
        "What is the latest Python version?",
        "And Node.js?",
        "What is the current Node.js version?",
    ),
    (
        "Who is the current president of France?",
        "What about Germany?",
        "Who is the current president of Germany?",
    ),
    (
        "When is the next F1 race?",
        "And MotoGP?",
        "When is the next MotoGP race?",
    ),
    (
        "Apple stock price?",
        "And Microsoft?",
        "What is the current stock price of Microsoft?",
    ),
    (
        "What is the latest Python version?",
        "And .NET?",
        "What is the current .NET version?",
    ),
    (
        "What is the latest .NET version?",
        "And C#?",
        "What is the current C# version?",
    ),
])
def test_followups_inherit_typed_predicate_and_replace_only_subject(
    previous, followup, query,
):
    history = [
        {"role": "user", "content": previous},
        {"role": "assistant", "content": "A tool-backed answer."},
    ]

    assert web_intents.classify(followup, history) == {
        "kind": "research", "query": query,
    }


@pytest.mark.parametrize("followup", [
    "And Node.js?",
    "What about Germany?",
    "What about that?",
    "What about tomorrow?",
    "And in 1995?",
])
def test_followups_without_safe_completed_context_stay_offline(followup):
    assert web_intents.classify(followup) is None


def test_intervening_topic_prevents_older_live_context_bleed():
    history = [
        {"role": "user", "content": "What is the latest Python version?"},
        {"role": "assistant", "content": "A current answer."},
        {"role": "user", "content": "Explain recursion."},
        {"role": "assistant", "content": "A static explanation."},
    ]

    assert web_intents.classify("And Node.js?", history) is None


def test_unanswered_user_turn_does_not_prime_followup():
    history = [
        {"role": "user", "content": "Who is the president of France?"},
    ]

    assert web_intents.classify("What about Germany?", history) is None


def test_repeated_assistant_turn_breaks_followup_adjacency():
    history = [
        {"role": "user", "content": "What is the latest Python version?"},
        {"role": "assistant", "content": "A current answer."},
        {"role": "assistant", "content": "An unrelated assistant notice."},
    ]

    assert web_intents.classify("And Node.js?", history) is None


@pytest.mark.parametrize("history", [
    [None],
    ["junk"],
    {"role": "assistant", "content": "not a list"},
    [{"role": "assistant", "content": ["multimodal", "content"]}],
    [{"role": "assistant", "content": "Who is the current CEO?"}],
])
def test_malformed_or_assistant_only_history_fails_closed(history):
    assert web_intents.classify("What about Germany?", history) is None


def test_chat_boundary_keeps_work_request_off_web(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server, "_agent_impl",
        lambda *args, **kwargs: pytest.fail("web agent must not run"),
    )

    assert server.chat_web_response(
        "Write code to fetch the current Bitcoin price."
    ) is None


def test_denial_guard_cannot_bypass_work_boundary(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server, "_agent_impl",
        lambda *args, **kwargs: pytest.fail("work prompt must not reach web agent"),
    )

    assert server._web_denial_guard(
        "Search the repo for current CEO mentions",
        "I can't browse the web.",
    ) is None


def test_explicit_web_search_still_overrides_work_classifier(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server.intents, "classify_work", lambda _prompt: True)
    monkeypatch.setattr(server, "_agent_impl", lambda *args, **kwargs: "SEARCHED")

    assert server.chat_web_response(
        "search the web for current Python news"
    ) == "SEARCHED"


@pytest.mark.parametrize("prompt", [
    "Do a web search for the current CEO of OpenAI",
    "Use web search for the current CEO of OpenAI",
    "Web search for the current CEO of OpenAI",
    "Please use the web to tell me who wrote Hamlet",
    "Search the web for how to build a PC",
    "Web search for ways to fix a leaking faucet",
])
def test_web_search_imperatives_bypass_work_gate(monkeypatch, prompt):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_agent_impl", lambda *args, **kwargs: "SEARCHED")

    assert server.chat_web_response(prompt) == "SEARCHED"


def test_web_search_widget_is_still_a_work_request(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server, "_agent_impl",
        lambda *args, **kwargs: pytest.fail("work prompt must not use web route"),
    )

    assert server.chat_web_response("Build a web search widget") is None


@pytest.mark.parametrize("prompt", [
    "Use a web search API to build the feature",
    "Use web search results to update the README",
    "Do a web search and update the file",
    "Web search for X; update README",
    "Search the web for X before updating README",
    "This app should search the web for current prices",
    "Make the app search the web for current prices",
    "Use the web to update README",
    "Use the browser to edit this file",
    "Search the web for X, update README",
    "Search the web for X; please update README",
    "Search the web for X then please update README",
    "Search the web for X. Update README",
    "Search the web for X\nUpdate README",
    "Use the web to find X so you can update README",
    "Use web to find X to update README",
    "Search the web, then delete C:\\Secret\\plan.txt",
    "Search the web, then remove src/private.ps1",
    "Search the web, then rename the private file",
    "Search the web, then move the private file",
    "Search the web, then run the private script",
    "Search the web, then execute the private script",
    "Search the web, then test the private project",
    "Search the web, then verify the private build",
    "Search the web, then deploy the private app",
    "Search the web, then make changes to the repo",
    "Search the web, then apply the fix",
    "Search the web, then patch the code",
    "Search the web; afterwards edit README.md",
    "Search the web and use what you find to update src/private.ps1",
])
def test_compound_web_search_work_stays_on_work_path(monkeypatch, prompt):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server, "_agent_impl",
        lambda *args, **kwargs: pytest.fail("compound work must not use web route"),
    )

    assert server.chat_web_response(prompt) is None


@pytest.mark.parametrize("prompt", [
    "What is the current version in C:\\Clients\\AcmeSecret\\app.config?",
    "What is the current version in our package.json?",
    "What is the latest dependency version in our requirements.txt?",
    "What is the current release in our internal app?",
    "Who is our current CEO?",
    "What is the latest API version in our service?",
    "What is the current version in appsettings.json?",
    "What is the current version in go.mod?",
    "What is the current version in .\\src\\config.yaml?",
    "What is the current version in \\\\server\\share\\version.txt?",
    "What is the current version in src/version.txt?",
    "What is the current version in config/version.txt?",
    "What is the current version in docs/VERSION?",
    "What is the current version in Gemfile?",
    "What is the current version from build.gradle?",
    "What is the current version in BUILD.bazel?",
    "What is the current version in Jenkinsfile?",
    "What is the current version in this project?",
    "What is the latest version in the project?",
    "What is the current version in this code?",
    "What is the current price in this JSON?",
    "What is the latest API version in this app?",
    "Which current version is configured here?",
    "What is the current version according to config.toml?",
    "What is the current version recorded by VERSION.txt?",
])
def test_private_artifact_questions_never_call_public_web(monkeypatch, prompt):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server, "_agent_impl",
        lambda *args, **kwargs: pytest.fail("private artifact leaked to web"),
    )

    assert server.chat_web_response(prompt) is None


@pytest.mark.parametrize("followup", [
    "And internal service?",
    "And proprietary app?",
    "And confidential project?",
    "And package.json?",
    "And Gemfile?",
])
def test_private_followup_subject_never_inherits_public_web_route(
    monkeypatch, followup,
):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server, "_agent_impl",
        lambda *args, **kwargs: pytest.fail("private followup leaked to web"),
    )
    history = [
        {"role": "user", "content": "What is the latest Python version?"},
        {"role": "assistant", "content": "A current tool-backed answer."},
    ]

    assert server.chat_web_response(followup, history=history) is None


def test_followup_dispatch_uses_resolved_dated_query_not_ellipsis(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    calls = []
    monkeypatch.setattr(
        server, "_agent_impl",
        lambda task, **kwargs: calls.append((task, kwargs)) or "RESEARCHED",
    )
    history = [
        {"role": "user", "content": "What is the latest Python version?"},
        {"role": "assistant", "content": "A current answer."},
    ]

    assert server.chat_web_response("And Node.js?", history=history) == "RESEARCHED"

    task, kwargs = calls[0]
    assert "current Node.js version" in task
    assert "And Node.js" not in task
    assert time.strftime("%B %Y") in task
    assert kwargs["required_tool_names"] == ("web_fetch",)


def test_disabled_web_reports_only_for_positive_direct_and_followup(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(
        server, "_agent_impl",
        lambda *args, **kwargs: pytest.fail("disabled web must not call agent"),
    )
    history = [
        {"role": "user", "content": "Who is the president of France?"},
        {"role": "assistant", "content": "A current answer."},
    ]
    disabled = "Web tools are disabled in the current runtime by SONDER_WEB_TOOLS."

    assert server.chat_web_response("Who is OpenAI's CEO?") == disabled
    assert server.chat_web_response("What about Germany?", history=history) == disabled
    assert server.chat_web_response("What does a CEO do?") is None


def test_session_route_persists_first_web_turn_and_resolves_second(
    monkeypatch, tmp_path,
):
    database = tmp_path / "memory.db"
    monkeypatch.setattr(server, "_open_db", lambda: server.memory_store.connect(database))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(
        server, "_answer",
        lambda *args, **kwargs: pytest.fail("plain model must not run"),
    )
    tasks = []
    monkeypatch.setattr(
        server, "_agent_impl",
        lambda task, **kwargs: tasks.append(task) or "TOOL-BACKED",
    )
    session = "volatile-followup"

    first = server._sonder_impl(
        "What is the latest Python version?", session=session, project="none",
    )
    second = server._sonder_impl("And Node.js?", session=session, project="none")

    assert "TOOL-BACKED" in first
    assert "TOOL-BACKED" in second
    assert "current Node.js version" in tasks[1]
    assert "[interaction_id" not in first + second
    conn = server.memory_store.connect(database)
    turns = server.memory_store.session_history(conn, session, max_turns=10)
    conn.close()
    assert [turn[0] for turn in turns] == [
        "What is the latest Python version?", "And Node.js?",
    ]


def test_upcoming_query_gets_fixed_freshness_anchor():
    now = 1767225600
    query = web_tools.build_search_query("When is the next F1 race?", now=now)

    assert query == "F1 race %s" % time.strftime("%B %Y", time.localtime(now))


@pytest.mark.parametrize("prompt", [
    "Search the web for CSS next sibling selector",
    "Search the web for what to do next after a git rebase",
    "Next.js documentation",
    "Search web for next game state pattern",
    "Search web for next match algorithm",
    "Search web for next episode button accessibility",
    "Search web for upcoming release branch naming",
    "Next.js release process",
    "regex next match",
    "how to get the next regex match",
    "iterator next match",
    "state machine next state match",
    "When is the next regex match?",
])
def test_unrelated_next_text_is_not_given_a_freshness_anchor(prompt):
    assert web_tools.build_search_query(prompt) == prompt


@pytest.mark.parametrize("prompt", [
    "Search the web for the latest CSS next sibling selector syntax",
    "Latest news about CSS next sibling selectors",
])
def test_next_keeps_semantic_meaning_during_other_recency_rewrite(prompt):
    assert "next" in web_tools.build_search_query(prompt).lower()


def test_public_pytest_release_queries_are_not_private_repo_work():
    assert web_intents.classify("What is the latest pytest version?") == {
        "kind": "research", "query": "What is the latest pytest version?",
    }
    assert web_intents.classify("What is the latest release of pytest?") == {
        "kind": "research", "query": "What is the latest release of pytest?",
    }


@pytest.mark.parametrize("name", ["C++", "C#", "F#", ".NET"])
def test_current_version_query_preserves_technical_language_name(name):
    query = web_tools.build_search_query(
        "What is the latest %s version?" % name,
        now=1767225600,
    )
    assert name in query


def test_technical_language_followup_keeps_subject_punctuation():
    history = [
        {"role": "user", "content": "What is the latest Python version?"},
        {"role": "assistant", "content": "A current answer."},
    ]
    intent = web_intents.classify("And C++?", history)

    assert intent == {"kind": "research", "query": "What is the current C++ version?"}
    assert "C++" in web_tools.build_search_query(intent["query"], now=1767225600)
