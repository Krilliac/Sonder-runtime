"""Coverage contracts for CredentialScanner.

All values are obviously fake. The scanner must catch the shapes real
projects actually contain (env-style names with '_' around the keyword, JSON
keys with a quote before the colon, well-known token prefixes).
"""
from __future__ import annotations

import time

import pytest

from sonder_runtime.application.security.secrets_and_fuzz import CredentialScanner


@pytest.mark.parametrize(
    "line",
    (
        "OPENAI_API_KEY=sk-FAKEFAKEFAKEFAKEFAKE",
        "DB_PASSWORD=hunter2-not-real",
        "GITHUB_TOKEN=abc",
        "AWS_SECRET_ACCESS_KEY=FAKEFAKEFAKEFAKE",
        '{"password": "hunter2-not-real"}',
        "'api_key': 'FAKE-not-real'",
        "sk-proj-FAKEFAKEFAKEFAKEFAKEFAKE",
        "key = sk-FAKEFAKEFAKEFAKEFAKEFAKE",
        "AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE",
        "xoxb-FAKE0000000000",
        "ghp_FAKEFAKEFAKEFAKEFAKEFAKE",
        "github_pat_FAKEFAKEFAKEFAKEFAKEFAKE",
        "AKIAFAKEFAKEFAKEFAKE",
        "ASIAFAKEFAKEFAKEFAKE",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "https://user:hunter2-not-real@example.invalid/repo.git",
    ),
)
def test_scanner_flags_common_secret_shapes(line):
    assert CredentialScanner().scan(line)


@pytest.mark.parametrize(
    "line",
    (
        'password = ""',
        "password = ''",
        "password: <your password here>",
        'password: "<your password here>"',
        "task-force-assignment-for-the-whole-team",
        "nothing to see here",
    ),
)
def test_scanner_ignores_placeholders_and_prose(line):
    assert CredentialScanner().scan(line) == ()


def test_scanner_never_returns_secret_text():
    findings = CredentialScanner().scan("DB_PASSWORD=hunter2-not-real")
    assert findings and all("hunter2" not in item.redacted for item in findings)


@pytest.mark.parametrize(
    "payload",
    (
        "token" * 10_000,
        "sk-" * 17_000,
        "password_" * 5_500,
        'password="' + "a" * 50_000,
        "ghp_" + "a" * 50_000,
        "-----BEGIN " + "A " * 25_000,
        "a://" * 12_500,
        "a://" + "b" * 50_000,
    ),
)
def test_scanner_is_linear_on_adversarial_single_line(payload):
    started = time.perf_counter()
    CredentialScanner().scan(payload)
    assert time.perf_counter() - started < 0.5


def test_scanner_matches_long_key_names_and_stays_linear():
    # Any prefix length; after the keyword only known secret suffixes
    # (_KEY, _ID, _ACCESS_KEY, ...) so "secrets_path" or "context_tokens" in
    # ordinary code are not findings.
    name = "MY_" + "X" * 100 + "_SERVICE_TOKEN_KEY_ID"
    assert CredentialScanner().scan(name + "=hunter2-not-real")
    started = time.perf_counter()
    CredentialScanner().scan("token" * 10_000)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "line",
    (
        'password = "' + "A" * 300 + '"',
        "secret: '" + "B" * 257 + "'",
        'token = "abc-not-real',
        "password = '''hunter2-not-real'''",
        'password = """hunter2-not-real"""',
        'password = """',
        "password" + " " * 20 + "= hunter2xyz",
        "password\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t= hunter2xyz",
        "password\xa0=\xa0hunter2xyz",
        "DB_PASSWORD=letmein",
        "password: hunter2",
        "password = 1234",
        "aws_secret_access_key = FAKEFAKEFAKEFAKE",
        "SECRET_KEY_BASE=FAKEFAKEFAKEFAKE",
        "token = b'FAKEFAKEFAKE'",
        "x-api-key: FAKE-not-real",
        "tokenapi-key=FAKE-not-real",
        'password := "hunter2-not-real"',
        "password:= hunter2",
        'raise ValueError("bad: API_KEY=abcd1234 rejected")',
        'api_key: str = "graph-held-secret-91b2"',
        '"http://127.0.0.1:8080/health?token=letmein"',
        'run("done password=swordfish Bearer x")',
        'write_text("TOKEN=letmein")',
        'f(name="x", password="hunter2-not-real")',
    ),
)
def test_scanner_flags_values_the_old_scanner_flagged(line):
    assert CredentialScanner().scan(line), line


@pytest.mark.parametrize(
    "line",
    (
        "secrets_path=_configured_path(",
        'secrets_path = getattr(args, "secrets", None)',
        'secrets_path = _configured_path(args.secrets, "SONDER_SECRETS", "sonder.env")',
        "token = values[index]",
        'if action is not None and token == "--json":',
        "context_tokens=args.context_tokens,",
        "max_tokens=4096,",
        'api_key = os.environ.get("OPENAI_API_KEY", "")',
        "password = getpass()",
        "token = None",
        "token=token,",
        "api_key=api_key,",
        "token = new_token",
        "password = DB_PASSWORD",
        'token: str = ""',
        "password: Optional[str] = None",
        "secret: SecretStr",
        "token: string;",
        "token := next(it)",
        "self.token = token",
        "token = self._token",
        "token = tokens[0]",
        "credential = (user, pw)",
        'token=token or "",',
        "principal_secret=principal_secret)",
        "api_key=key, base_url=settings.base_url,",
        "chars_per_token: float = 3.5",
        'ENV_API_KEY = "OPENROUTER_API_KEY"',
        '"AND claim_token=? AND owner_pid=?",',
        "def release(self, token: str) -> None:",
        "username, password = parts",
        'TOKEN_MARKER = "token: "',
    ),
)
def test_scanner_ignores_code_that_merely_names_a_secret(line):
    assert CredentialScanner().scan(line) == (), line


def _best_of_three(payload):
    best = None
    for _ in range(3):
        started = time.perf_counter()
        CredentialScanner(max_bytes=10_000_000).scan(payload)
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return best


@pytest.mark.parametrize(
    "unit",
    ("token", "token ", 'password="', "password = '", 'token = """', "x-api-", "-key",
     "token" + " " * 30, "a=", "token=a.", "token=a(", "token:", "token = x\xa0",
     "a" * 40 + "_token = b ", "password\xa0" * 3),
)
def test_scanner_stays_linear_at_10_20_50_kb(unit):
    def payload(size):
        return (unit * (size // len(unit) + 1))[:size]

    # A linear scan grows ~5x from 10 to 50 KB, a quadratic one ~25x.
    t10, t20, t50 = (_best_of_three(payload(kb * 1000)) for kb in (10, 20, 50))
    assert t50 <= max(0.005, 12 * t10), (unit, t10, t20, t50)
