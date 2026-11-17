import pytest

from sonder_runtime.platform import child_environment_policy
from sonder_runtime.platform import logging as runtime_logging
from sonder_runtime.platform.config import SECRET_ENV_KEYS


def test_child_secret_policy_owns_classification():
    assert runtime_logging._unsafe_child_secret_name is child_environment_policy.unsafe_child_secret_name


def test_logging_secret_environment_list_covers_typed_config_secrets():
    assert set(SECRET_ENV_KEYS) <= set(runtime_logging.SECRET_ENV_VARS)


def test_child_secret_policy_rejects_control_and_secret_names():
    unsafe = ("SONDER_API_KEY", "DATABASE_URL", "BUILD_APPROVAL", "CUSTOM_TOKEN")
    assert all(child_environment_policy.unsafe_child_secret_name(name) for name in unsafe)


def test_child_secret_policy_allows_unrelated_environment_names():
    safe = ("PATH", "LANG", "PYTHONIOENCODING", "WORKSPACE_ROOT")
    assert not any(child_environment_policy.unsafe_child_secret_name(name) for name in safe)


@pytest.mark.parametrize(
    "secret_name",
    tuple(dict.fromkeys((*SECRET_ENV_KEYS, *runtime_logging.SECRET_ENV_VARS))),
)
def test_child_environment_strips_every_config_secret_name(secret_name):
    child = runtime_logging.child_environment(
        {"PATH": "safe-path", secret_name: "private-config-value"}
    )

    assert child == {"PATH": "safe-path"}


def test_child_environment_strips_generic_authority_names_outside_unsafe_lab():
    child = runtime_logging.child_environment({
        "PATH": "safe-path",
        "DATABASE_URL": "postgres://private",
        "CUSTOM_CONTROL_GATE": "authority",
        "SONDER_HOME": "private-runtime-home",
    })

    assert child == {"PATH": "safe-path"}


@pytest.mark.parametrize("name", ("GITHUB_PAT", "OPENWEATHERMAP_APIKEY", "SENTRY_DSN", "MY_PAT_VALUE", "PAT"))
def test_child_secret_policy_covers_pat_apikey_and_dsn_names(name):
    assert child_environment_policy.unsafe_child_secret_name(name)


@pytest.mark.parametrize("name", ("PATH", "PATHEXT", "PYTHONPATH", "PATTERN", "COMPATIBLE", "DSNAME", "PATCH_LEVEL"))
def test_child_secret_policy_matches_pat_and_dsn_on_segments_only(name):
    assert not child_environment_policy.unsafe_child_secret_name(name)


def test_child_environment_strips_url_values_that_embed_a_password():
    child = runtime_logging.child_environment({
        "PATH": "safe-path",
        "MONGODB_URI": "mongodb://user:hunter2-not-real@db.invalid/app",
        "REDIS_URL": "redis://default:hunter2-not-real@cache.invalid:6379",
        "GITHUB_PAT": "ghp_FAKEFAKEFAKEFAKE",
        "OPENWEATHERMAP_APIKEY": "FAKE",
        "SENTRY_DSN": "https://FAKE@o0.ingest.invalid/1",
        "SERVICE_URL": "https://example.invalid/path",
        "REDIS_URI": "redis://cache.invalid:6379",
        "NOTE": "mongodb://user:hunter2-not-real@db.invalid/app",
    })

    assert child == {
        "PATH": "safe-path",
        "SERVICE_URL": "https://example.invalid/path",
        "REDIS_URI": "redis://cache.invalid:6379",
    }


@pytest.mark.parametrize(
    "value",
    (
        "https://ghp_FAKEFAKEFAKE@github.invalid",
        "redis://default:pa/ss-not-real@cache.invalid:6379",
        "redis://default:" + "p" * 300 + "@cache.invalid:6379",
        "postgres://user:p%40ss-not-real@db.invalid/app",
        "https://:onlypass-not-real@db.invalid",
        "https://[broken-not-real@db.invalid",
    ),
)
def test_child_environment_strips_any_userinfo_in_url_values(value):
    assert runtime_logging.child_environment({"PATH": "p", "SERVICE_URL": value}) == {"PATH": "p"}


@pytest.mark.parametrize(
    "value",
    ("https://example.invalid/path", "https://example.invalid/?a=b@c", "redis://cache.invalid:6379/0", "", "localhost:6379"),
)
def test_child_environment_keeps_url_values_without_userinfo(value):
    child = runtime_logging.child_environment({"PATH": "p", "SERVICE_URL": value})
    assert child == {"PATH": "p", "SERVICE_URL": value}


@pytest.mark.parametrize(
    "name, value",
    (
        ("HTTP_PROXY", "http://user:hunter2-not-real@proxy.invalid:8080"),
        ("HTTPS_PROXY", "http://user:hunter2-not-real@proxy.invalid:8080"),
        ("ALL_PROXY", "socks5://user:hunter2-not-real@proxy.invalid:1080"),
        ("DATABASE", "postgres://user:hunter2-not-real@db.invalid/app"),
        ("REDIS", "redis://default:hunter2-not-real@cache.invalid:6379"),
        ("CELERY_BROKER", "amqp://user:hunter2-not-real@mq.invalid//"),
        ("SERVICE_URLS", "https://ok.invalid,https://user:hunter2-not-real@b.invalid"),
        ("MONGOURI", "mongodb://user:hunter2-not-real@db.invalid/app"),
        ("NOTE", "mongodb://user:hunter2-not-real@db.invalid/app"),
        ("GIT_REMOTE", "https://ghp_FAKEFAKEFAKEFAKE@github.invalid/o/r.git"),
    ),
)
def test_child_environment_strips_userinfo_urls_under_any_name(name, value):
    assert child_environment_policy.unsafe_child_environment_entry(name, value)
    assert runtime_logging.child_environment({"PATH": "p", name: value}) == {"PATH": "p"}


@pytest.mark.parametrize(
    "name",
    ("PATH", "PATHEXT", "PYTHONPATH", "NODE_PATH", "HOMEPATH", "PSModulePath",
     "COMSPEC", "SYSTEMROOT", "VIRTUAL_ENV", "PYTHONHOME"),
)
def test_child_environment_never_strips_search_path_variables(name):
    value = "C:\\tools;file://user:pw@host/share"
    assert not child_environment_policy.unsafe_child_environment_entry(name, value)


@pytest.mark.parametrize(
    "name, value",
    (
        ("HTTP_PROXY", "http://proxy.invalid:8080"),
        ("NO_PROXY", "localhost,127.0.0.1"),
        ("EMAIL", "me@example.invalid"),
        ("HOMEPAGE", "https://example.invalid/docs?a=b@c"),
        ("LANG", "en_US.UTF-8"),
    ),
)
def test_child_environment_keeps_values_without_url_userinfo(name, value):
    assert not child_environment_policy.unsafe_child_environment_entry(name, value)
