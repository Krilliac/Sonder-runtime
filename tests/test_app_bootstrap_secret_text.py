"""The app must not claim the server prints the first-admin bootstrap secret.

The server never prints it: ``admin_auth.register`` reads it from the
``SONDER_BOOTSTRAP_SECRET`` environment variable.
"""

import re
from pathlib import Path

import admin_auth

APP_LIB = Path(__file__).resolve().parents[1] / "app" / "lib"
# Any wording that puts "print"/"printed" (not negated as "not print") within 120
# characters of "secret", either order, across string-literal line breaks.
CLAIM = re.compile(
    r"(?<!not )\bprint(?:ed|s)?\b[\s\S]{0,120}secret|secret[\s\S]{0,120}(?<!not )\bprint",
    re.IGNORECASE,
)
FORBIDDEN = ("shown by the server", "server printed", "printed by the server")


def test_server_reads_secret_from_environment_not_output():
    assert admin_auth._bootstrap_secret.__code__.co_consts
    assert "SONDER_BOOTSTRAP_SECRET" in Path(admin_auth.__file__).read_text(encoding="utf-8")


def test_app_does_not_claim_server_prints_secret():
    for path in sorted(APP_LIB.rglob("*.dart")):
        text = path.read_text(encoding="utf-8")
        for phrase in FORBIDDEN:
            assert phrase not in text.lower(), f"{path.name}: {phrase!r}"
        match = CLAIM.search(text)
        assert match is None, f"{path.name}: {match and match.group(0)!r}"


def test_app_remedy_names_the_environment_variable():
    text = (APP_LIB / "api" / "transport.dart").read_text(encoding="utf-8")
    assert "SONDER_BOOTSTRAP_SECRET" in text
