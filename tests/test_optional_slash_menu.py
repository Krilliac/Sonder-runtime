from sonder_runtime.adapters.optional_slash_menu import load_optional_slash_menu


def test_optional_slash_menu_loads_when_present():
    menu = load_optional_slash_menu()
    assert menu is not None
    assert callable(menu.available)
