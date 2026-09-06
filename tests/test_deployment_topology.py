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
