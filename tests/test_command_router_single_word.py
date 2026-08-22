"""Natural-language routing for unambiguous one-word catalog commands."""

import command_router as cr


def test_read_requests_reach_native_single_word_commands():
    assert cr.resolve("show the agents") == "/agents"
    assert cr.resolve("show the fanouts") == "/fanouts"
    assert cr.resolve("show the model") == "/model"
    assert cr.resolve("show the context") == "/context"


def test_single_word_lifecycle_and_toggle_commands_stay_out_of_generic_routing():
    assert cr.resolve("show the exit") is None
    assert cr.resolve("show the strict") is None
    assert cr.resolve("show the trace") is None


def test_one_word_required_arguments_preserve_path_like_tokens():
    assert cr.resolve("get weather in Chicago") == "/weather Chicago"
    assert cr.resolve("inspect artifactcheck report.json") == \
        "/artifactcheck report.json"
    assert cr.resolve("inspect artifactcheck /tmp/report.json") == \
        "/artifactcheck /tmp/report.json"
    assert cr.resolve("inspect artifactcheck ./report.json") == \
        "/artifactcheck ./report.json"
    assert cr.resolve("inspect artifactcheck ../report.json") == \
        "/artifactcheck ../report.json"
    assert cr.resolve("inspect artifactcheck ~/report.json") == \
        "/artifactcheck ~/report.json"
    assert cr.resolve("inspect artifactcheck report.json and summarize it") is None
