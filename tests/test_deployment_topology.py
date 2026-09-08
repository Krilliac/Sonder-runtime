from dataclasses import replace

import pytest

from sonder_runtime.platform.config import ConfigError, SonderConfig, load_config


def test_default_deployment_reports_single_host_profile():
    config = load_config(env={})
    assert getattr(config, 'deployment', None) is not None, 'deployment contract is missing'
    assert config.deployment.profile == 'single-host'


@pytest.mark.parametrize('setting', ['automatic_takeover', 'automatic_failback'])
def test_unsupported_automatic_ha_fails_configuration_with_precise_reason(tmp_path, setting):
    path = tmp_path / 'sonder.toml'
    path.write_text(f'[deployment]\n{setting}=true\n', encoding='utf-8')
    with pytest.raises(ConfigError, match='fencing.*acknowledged.*replication'):
        load_config(path, env={})

@pytest.mark.parametrize('setting', ['automatic_takeover', 'automatic_failback'])
def test_direct_application_startup_cannot_bypass_ha_validation(setting):
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import DeploymentConfig
    config = SonderConfig(deployment=DeploymentConfig(**{setting: True}))
    with pytest.raises(ConfigError, match='ownership-epoch'):
        build_application(config=config)


def test_configured_pair_reports_pooling_without_claiming_authority_or_replication(tmp_path, monkeypatch):
    from sonder_runtime.adapters.web import lifecycle
    from sonder_runtime.platform.config import ComputeConfig, ComputeNodeConfig, DeploymentConfig
    monkeypatch.setenv('SONDER_HOME', str(tmp_path))
    monkeypatch.setattr(lifecycle.RuntimeLifecycle, 'adopt_legacy_start', lambda self: None)
    config = SonderConfig(deployment=DeploymentConfig(profile='pooled-pair', preferred_primary='primary'),
        compute=ComputeConfig(node_id='secondary', allow_remote=True, nodes=(
            ComputeNodeConfig(node_id='primary', origin='https://primary:11435'),)))
    lifecycle.reset_for_tests()
    try:
        lifecycle.configure(config)
        status = lifecycle.get().health_payload().get('deployment')
        assert status is not None, 'production health lacks deployment capability report'
        assert status['profile'] == 'pooled-pair'
        assert status['configured_members'] == ['secondary', 'primary']
        assert status['preferred_primary'] == 'primary'
        assert status['control_state_scope'] == 'local-instance'
        capabilities = status['capabilities']
        assert capabilities['private_compute']['available'] is True
        for key in ('automatic_takeover', 'automatic_failback', 'explicit_promotion',
                    'acknowledged_state_replication', 'worker_epoch_fencing', 'quorum'):
            assert capabilities[key]['available'] is False
            assert capabilities[key]['reason']
    finally:
        lifecycle.reset_for_tests()


def test_direct_lifecycle_configure_cannot_bypass_ha_validation():
    from sonder_runtime.adapters.web import lifecycle
    from sonder_runtime.platform.config import DeploymentConfig
    with pytest.raises(ConfigError, match='independent old-owner fencing'):
        lifecycle.configure(SonderConfig(deployment=DeploymentConfig(automatic_takeover=True)))


def test_typed_lifecycle_reconfiguration_retires_the_prior_probe(tmp_path, monkeypatch):
    """A new config cannot orphan the prior lifecycle's endpoint probe."""
    from sonder_runtime.adapters.web import lifecycle
    from sonder_runtime.platform.config import StateConfig

    lifecycle.reset_for_tests()
    monkeypatch.setattr(
        lifecycle.RuntimeLifecycle, "probe_ollama_once", lambda self, timeout=None: True,
    )
    first_config = SonderConfig()
    second_config = SonderConfig(state=StateConfig(home=str(tmp_path / "next")))
    try:
        lifecycle.configure(first_config)
        first = lifecycle.get()
        first.begin_ollama_probe(interval_seconds=60)
        assert first._probe_thread is not None and first._probe_thread.is_alive()

        lifecycle.configure(second_config)

        assert first._probe_stop.is_set()
        assert not first._probe_thread.is_alive()
        assert lifecycle.get() is not first
    finally:
        lifecycle.reset_for_tests()


def test_typed_lifecycle_reconfiguration_refuses_an_unjoined_prior_probe(tmp_path):
    """A blocked probe retains its exact lifecycle instead of being orphaned."""
    from threading import Event, Thread

    from sonder_runtime.adapters.web import lifecycle
    from sonder_runtime.platform.config import StateConfig

    lifecycle.reset_for_tests()
    first_config = SonderConfig()
    second_config = SonderConfig(state=StateConfig(home=str(tmp_path / "next")))
    release = Event()
    thread = None
    try:
        lifecycle.configure(first_config)
        first = lifecycle.get()
        first._ollama_probe_timeout = 0
        thread = Thread(target=release.wait, daemon=True)
        thread.start()
        first._probe_thread = thread

        with pytest.raises(RuntimeError, match="probe did not stop"):
            lifecycle.configure(second_config)

        assert lifecycle._configured_config is first_config
        assert lifecycle._instance is first
        assert thread.is_alive()
    finally:
        release.set()
        if thread is not None:
            thread.join(timeout=5)
        lifecycle.reset_for_tests()


def test_typed_lifecycle_reconfiguration_stops_an_inflight_probe_before_publish(
    tmp_path, monkeypatch,
):
    """A probe starting during reconfiguration cannot publish after retirement."""
    from threading import Event, Thread

    from sonder_runtime.adapters.web import lifecycle
    from sonder_runtime.platform.config import StateConfig

    lifecycle.reset_for_tests()
    first_config = SonderConfig()
    second_config = SonderConfig(state=StateConfig(home=str(tmp_path / "next")))
    entered = Event()
    release = Event()
    errors = []
    starter = None
    replacer = None

    def blocked_probe(self, timeout=None):
        entered.set()
        assert release.wait(5)
        return True

    def replace_lifecycle():
        try:
            lifecycle.configure(second_config)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(lifecycle.RuntimeLifecycle, "probe_ollama_once", blocked_probe)
    try:
        lifecycle.configure(first_config)
        first = lifecycle.get()
        first._ollama_probe_timeout = 5
        starter = Thread(target=first.begin_ollama_probe)
        starter.start()
        assert entered.wait(5)

        replacer = Thread(target=replace_lifecycle)
        replacer.start()
        assert first._probe_stop.wait(5)
        release.set()
        starter.join(timeout=5)
        replacer.join(timeout=5)

        assert not starter.is_alive()
        assert not replacer.is_alive()
        assert errors == []
        assert first._probe_thread is None
        assert lifecycle.get() is not first
    finally:
        release.set()
        if starter is not None:
            starter.join(timeout=5)
        if replacer is not None:
            replacer.join(timeout=5)
        lifecycle.reset_for_tests()


@pytest.mark.parametrize('profile,peer_count,valid', [('single-host', 0, True), ('single-host', 1, True),
    ('single-host', 2, True), ('pooled-pair', 0, False), ('pooled-pair', 1, True),
    ('pooled-pair', 2, False), ('preferred-primary-ha', 1, False), ('quorum', 2, False)])
def test_profiles_validate_membership_without_restricting_legacy_compute_pool(profile, peer_count, valid):
    from sonder_runtime.platform.config import ComputeConfig, ComputeNodeConfig, DeploymentConfig, validate_deployment
    config = SonderConfig(deployment=DeploymentConfig(profile=profile), compute=ComputeConfig(nodes=tuple(
        ComputeNodeConfig(node_id=f'peer-{i}') for i in range(peer_count))))
    if valid:
        validate_deployment(config)
    else:
        with pytest.raises(ConfigError):
            validate_deployment(config)


def test_preference_must_name_member_and_confers_no_authority():
    from sonder_runtime.platform.config import DeploymentConfig, validate_deployment
    with pytest.raises(ConfigError, match='configured member'):
        validate_deployment(SonderConfig(deployment=DeploymentConfig(preferred_primary='unknown')))


def test_pooled_pair_toml_roundtrip(tmp_path):
    path = tmp_path / 'sonder.toml'
    path.write_text('''
[deployment]
profile = "pooled-pair"
preferred_primary = "main"
automatic_takeover = false
automatic_failback = false
[compute]
node_id = "worker"
allow_remote = true
[[compute.nodes]]
id = "main"
origin = "https://main:11435"
workloads = ["test"]
''', encoding='utf-8')
    config = load_config(path, env={'SONDER_API_KEY': 'x' * 24})
    assert config.as_redacted_dict()['deployment'] == {
        'profile': 'pooled-pair', 'preferred_primary': 'main',
        'automatic_takeover': False, 'automatic_failback': False,
    }


def test_default_health_status_does_not_infer_ha_from_local_readiness():
    from sonder_runtime.domain.deployment_topology import DeploymentStatus
    status = DeploymentStatus().as_dict()
    assert status['configured_members'] == ['local']
    assert status['capabilities']['local_sqlite_state']['available']
    assert not status['capabilities']['automatic_takeover']['available']
    assert not status['preference_confers_authority']


def test_recovery_posture_keeps_takeover_and_failback_fenced_without_a_witness():
    from sonder_runtime.domain.deployment_topology import DeploymentStatus

    status = DeploymentStatus(
        profile='pooled-pair',
        local_node='pc-a',
        peers=('pc-b',),
        allow_remote_compute=True,
    ).as_dict()

    posture = status['recovery_posture']
    assert posture['mode'] == 'external-authority-required'
    assert posture['automatic_takeover_available'] is False
    assert posture['automatic_failback_available'] is False
    assert posture['independent_witness_required'] is True
    assert 'independent witness' in posture['reason']
    assert status['capabilities']['automatic_takeover'] == {
        'available': False,
        'reason': posture['reason'],
    }
    assert status['capabilities']['automatic_failback'] == {
        'available': False,
        'reason': posture['reason'],
    }


def test_deployment_dashboard_projection_is_read_only_and_probe_free(monkeypatch):
    from sonder_runtime.adapters.web import lifecycle

    lifecycle.reset_for_tests()
    try:
        instance = lifecycle.get()
        monkeypatch.setattr(
            instance,
            'adopt_legacy_start',
            lambda: pytest.fail('dashboard projection must not adopt legacy state'),
        )
        projection = instance.deployment_payload()
        assert projection['profile_id'] == 'single-pc'
        assert projection['configured_members'] == ['local']
        assert projection['capabilities']['automatic_takeover']['available'] is False
    finally:
        lifecycle.reset_for_tests()
