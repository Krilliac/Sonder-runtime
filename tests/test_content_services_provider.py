from sonder_runtime.adapters.content_services import (
    FeedbackClassifierProvider,
    IntentClassifierProvider,
    TrainingTasksProvider,
)


def test_content_providers_resolve_root_helpers_dynamically(monkeypatch):
    import feedback
    import intents
    import training_tasks

    training_marker = object()
    signal_marker = object()
    intent_marker = object()
    monkeypatch.setattr(training_tasks, "sample", lambda count: training_marker)
    monkeypatch.setattr(feedback, "classify_signal", lambda content: signal_marker)
    monkeypatch.setattr(intents, "classify", lambda content: intent_marker)

    assert TrainingTasksProvider().sample(1) is training_marker
    assert FeedbackClassifierProvider().classify_signal("x") is signal_marker
    assert IntentClassifierProvider().classify("x") is intent_marker
