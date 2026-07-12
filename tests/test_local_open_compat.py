import sonder_serve


def test_local_open_allows_local_slash_commands():
    assert sonder_serve._developer_authorized({"mode": "local-open", "api_key": False, "account": None})
