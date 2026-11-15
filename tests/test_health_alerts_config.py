import pytest

from sonder_runtime.bootstrap.health_alerts import build_notifier, build_policy, run_health_alerts
from sonder_runtime.platform.config import ConfigError, SonderConfig, load_config

URL = "https://ntfy.example.invalid/topic-s3cr3t"


def load(tmp_path, toml="", env=None):
    path = tmp_path / "sonder.toml"
    path.write_text(toml, encoding="utf-8")
    return load_config(path, env=env or {})


def test_defaults_toast_on_ntfy_off_no_watched_jobs():
    c = SonderConfig().health_alerts
    assert c.toast is True and c.ntfy is False
    assert c.watched_jobs == () and c.known_off == ()


def test_section_loads_from_toml(tmp_path):
    config = load(tmp_path, '[health_alerts]\nwatched_jobs = ["A=5", "B"]\nknown_off = ["SonderNightly"]\n'
                            'rate_limit_seconds = 600\n')
    policy = build_policy(config)
    assert [(w.name, w.window_hours) for w in policy.watched_tasks] == [("A", 5.0), ("B", 26.0)]
    assert policy.known_off == ("SonderNightly",) and policy.rate_limit_seconds == 600.0


def test_url_comes_only_from_environment_and_is_redacted(tmp_path):
    config = load(tmp_path, '[health_alerts]\nntfy = true\n', env={"SONDER_HEALTH_ALERT_NTFY_URL": URL})
    assert config.secrets.health_alert_ntfy_url == URL
    assert "s3cr3t" not in repr(config)
    assert "s3cr3t" not in str(config.secrets.as_redacted_dict())
    assert "s3cr3t" not in str(config.as_redacted_dict())


def test_url_in_toml_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        load(tmp_path, '[health_alerts]\nhealth_alert_ntfy_url = "%s"\n' % URL)


def test_ntfy_enabled_without_url_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="ntfy enabled"):
        load(tmp_path, "[health_alerts]\nntfy = true\n")


def test_bad_url_scheme_error_does_not_echo_the_url(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, "", env={"SONDER_HEALTH_ALERT_NTFY_URL": "ftp://h/s3cr3t"})
    assert "s3cr3t" not in str(caught.value)


@pytest.mark.parametrize("line", ["rate_limit_seconds = 5", "default_window_hours = 0",
                                  "timeout_seconds = 0"])
def test_out_of_range_values_are_config_errors(tmp_path, line):
    with pytest.raises(ConfigError):
        load(tmp_path, "[health_alerts]\n%s\n" % line)


def test_ntfy_channel_is_built_only_when_enabled_and_url_set(tmp_path):
    off = build_notifier(load(tmp_path, "", env={"SONDER_HEALTH_ALERT_NTFY_URL": URL}))
    on = build_notifier(load(tmp_path, "[health_alerts]\nntfy = true\n",
                             env={"SONDER_HEALTH_ALERT_NTFY_URL": URL}))
    assert len(off._channels) == 1 and len(on._channels) == 2


def test_run_health_alerts_uses_injected_service():
    class Service:
        def check(self, report, reader):
            self.args = (report, reader)
            return "run"

    service = Service()
    assert run_health_alerts(SonderConfig(), {"checks": []}, service=service) == "run"
    assert service.args[0] == {"checks": []} and hasattr(service.args[1], "read")
