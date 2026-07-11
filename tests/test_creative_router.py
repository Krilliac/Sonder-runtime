import creative_router


def test_routes_explicit_cpp_isometric_rpg_to_grounded_game():
    intent = creative_router.classify(
        "Create a C++ 2.5D isometric Diablo-like RPG game with in-house assets.",
        mode="delegate",
    )

    assert intent["kind"] == "game"
    assert intent["language"] == "cpp"
    assert intent["dimension"] == "2.5d"
    assert intent["language_explicit"] is True
    assert intent["dimension_explicit"] is True
    assert intent["theme"] == "ember"
    assert "diablo" in intent["name"]
    assert len(intent["name"].split("-")[-1]) == 7


def test_fleet_or_multilanguage_game_request_routes_campaign():
    fleet = creative_router.classify(
        "Build 8 different games across various languages.", mode="fleet",
    )

    assert fleet["kind"] == "game_campaign"
    assert fleet["total"] == 8
    assert fleet["language_explicit"] is False
    assert fleet["dimension_explicit"] is False


def test_targeted_fleet_preserves_explicit_language_and_dimension():
    fleet = creative_router.classify(
        "Build 6 C++ 2.5D games as a parallel fleet.", mode="fleet",
    )

    assert fleet["kind"] == "game_campaign"
    assert fleet["language"] == "cpp"
    assert fleet["dimension"] == "2.5d"
    assert fleet["language_explicit"] is True
    assert fleet["dimension_explicit"] is True


def test_dimension_digits_are_not_mistaken_for_campaign_count():
    fleet = creative_router.classify(
        "Build a fleet of C++ 2.5D games.", mode="fleet",
    )

    assert fleet["total"] == 4


def test_routes_general_non_game_assets():
    intent = creative_router.classify(
        "Generate a frost brand kit with logo, music, diagrams, and a 3D model."
    )

    assert intent["kind"] == "artifact"
    assert intent["dimension"] == "3d"
    assert intent["theme"] == "frost"


def test_routes_explicit_humanoid_character_to_artifact_forge():
    intent = creative_router.classify(
        "Create a humanoid character with a 17-bone rig and animation clips."
    )

    assert intent["kind"] == "artifact"
    assert intent["dimension"] == "3d"


def test_does_not_hijack_questions_or_design_only_requests():
    assert creative_router.classify("How do I build a C++ game?") is None
    assert creative_router.classify("Design an isometric RPG combat system.") is None
    assert creative_router.classify("Review this existing game repository.") is None


def test_names_are_stable_and_bounded():
    task = "Create a compact Python 2D dungeon crawler game with generated sprites."

    first = creative_router.classify(task)
    second = creative_router.classify(task)

    assert first["name"] == second["name"]
    assert len(first["name"]) <= 56
