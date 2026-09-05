from concurrent.futures import ThreadPoolExecutor

import pytest

from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.domain.common.errors import CapacityExceeded, Conflict


def test_admission_requires_atomic_durable_worker_reservation(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db')
    assert callable(getattr(registry, 'reserve_capacity', None)), 'durable worker admission is missing'


def test_concurrent_connections_cannot_oversubscribe_and_restart_keeps_dispatch(tmp_path):
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    path = tmp_path / 'jobs.db'
    now = ['2026-09-04T12:00:00+00:00']
    registries = [SQLiteDurableJobRegistry(path, clock=lambda: now[0]) for _ in range(6)]
    budget = WorkerBudget('host', 100, 2)
    def admit(i):
        try:
            return registries[i].reserve_capacity(budget, f'job-{i}', str(i) * 64, 60, lease_seconds=10)
        except CapacityExceeded:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        accepted = [x for x in pool.map(admit, range(6)) if x is not None]
    assert len(accepted) == 1
    lease = accepted[0]
    registries[0].dispatch_capacity(lease.job_id, lease.token)
    now[0] = '2026-09-04T13:00:00+00:00'
    restarted = SQLiteDurableJobRegistry(path, clock=lambda: now[0])
    with pytest.raises(CapacityExceeded):
        restarted.reserve_capacity(budget, 'new', 'a' * 64, 60)
    restarted.release_capacity(lease.job_id)
    assert restarted.reserve_capacity(budget, 'new', 'a' * 64, 60).job_id == 'new'


def test_expired_admission_cannot_dispatch_or_conflicting_digest_rebind(tmp_path):
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    now = ['2026-09-04T12:00:00+00:00']
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db', clock=lambda: now[0])
    budget = WorkerBudget('host', 100, 2)
    lease = registry.reserve_capacity(budget, 'one', 'a' * 64, None, lease_seconds=1)
    assert lease.memory_bytes == 100
    assert registry.reserve_capacity(budget, 'one', 'a' * 64, None, lease_seconds=1) == lease
    with pytest.raises(Conflict):
        registry.reserve_capacity(budget, 'one', 'b' * 64, None)
    now[0] = '2026-09-04T12:00:02+00:00'
    with pytest.raises(Conflict):
        registry.dispatch_capacity('one', lease.token)
    registry.reserve_capacity(budget, 'two', 'c' * 64, 100)
    with pytest.raises(Conflict):
        registry.reserve_capacity(budget, 'one', 'b' * 64, None)

def test_process_request_carries_admission_token():
    from sonder_runtime.application.execution.process_jobs import ProcessJobRequest
    assert 'capacity_token' in ProcessJobRequest.__dataclass_fields__


def test_capacity_config_is_explicit_and_unknown_budget_fails_closed(tmp_path):
    from sonder_runtime.platform.config import load_config
    config = load_config(env={})
    assert config.compute.worker_memory_budget_bytes is None
    assert config.compute.worker_max_jobs == 1

@pytest.mark.parametrize('complete', [True, False])
def test_process_owner_releases_only_proven_containment_cleanup(tmp_path, complete):
    from dataclasses import replace
    from tests.test_job004_process_provider import _request, _Process, _Cleanup, _ScopedLimiter, _ScopedToken
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from sonder_runtime.adapters.extensions.memory_limits import ProcessContainmentResult
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db')
    budget = WorkerBudget('host', 100)
    lease = registry.reserve_capacity(budget, 'job-process', 'a' * 64, 100)
    token = _ScopedToken(ProcessContainmentResult(complete), ProcessContainmentResult(True))
    provider = SubprocessJobProvider(registry, process_cleanup=_Cleanup(complete=complete),
        launcher=lambda *a, **kw: _Process(), memory_limiter=_ScopedLimiter(token), cleanup_retry_seconds=60)
    provider.start(replace(_request(), require_job_scope=True, capacity_token=lease.token))
    provider.wait('job-process')
    if complete:
        registry.reserve_capacity(budget, 'new', 'b' * 64, 100)
    else:
        with pytest.raises(CapacityExceeded):
            registry.reserve_capacity(budget, 'new', 'b' * 64, 100)
        provider.cancel('job-process')
        registry.reserve_capacity(budget, 'new', 'b' * 64, 100)


def test_expired_token_prevents_process_dispatch(tmp_path):
    from dataclasses import replace
    from tests.test_job004_process_provider import _request, _Cleanup, _ScopedLimiter, _ScopedToken
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    now = ['2026-09-04T12:00:00+00:00']
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db', clock=lambda: now[0])
    lease = registry.reserve_capacity(WorkerBudget('host', 100), 'job-process', 'a' * 64, 100, lease_seconds=1)
    now[0] = '2026-09-04T12:00:02+00:00'
    launched = []
    provider = SubprocessJobProvider(registry, process_cleanup=_Cleanup(complete=True),
        launcher=lambda *a, **kw: launched.append(True), memory_limiter=_ScopedLimiter(_ScopedToken()))
    with pytest.raises(Conflict):
        provider.start(replace(_request(), require_job_scope=True, capacity_token=lease.token))
    assert launched == []


def test_ambiguous_launcher_failure_retains_occupied_reservation(tmp_path):
    from dataclasses import replace
    from tests.test_job004_process_provider import _request, _Cleanup, _ScopedLimiter, _ScopedToken
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db')
    budget = WorkerBudget('host', 100)
    lease = registry.reserve_capacity(budget, 'job-process', 'a' * 64, 100)
    def launch(*args, **kwargs):
        raise RuntimeError('dispatch result unknown')
    provider = SubprocessJobProvider(registry, process_cleanup=_Cleanup(complete=True),
        launcher=launch, memory_limiter=_ScopedLimiter(_ScopedToken()))
    with pytest.raises(RuntimeError, match='unknown'):
        provider.start(replace(_request(), require_job_scope=True, capacity_token=lease.token))
    with pytest.raises(CapacityExceeded):
        SQLiteDurableJobRegistry(tmp_path / 'jobs.db').reserve_capacity(budget, 'new', 'b' * 64, 100)
    provider.cancel('job-process')
    with pytest.raises(CapacityExceeded):
        registry.reserve_capacity(budget, 'after-cancel', 'c' * 64, 100)


def test_worker_reserves_explicit_catalog_demand_not_process_limit(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.test_compute_job_worker import _entry, _envelope, CapturingProvider
    from sonder_runtime.application.compute_fabric.jobs import ComputeJobWorker
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db')
    monkeypatch.setattr(ComputeJobWorker, '_artifact_stage_base', staticmethod(lambda: tmp_path / 'artifacts'))
    provider = CapturingProvider()
    worker = ComputeJobWorker(worker_id='worker', catalog={'pytest': replace(_entry(), memory_reservation_bytes=60)},
        workspace_mappings={'sonder': tmp_path}, provider=provider, capacity=registry, budget=WorkerBudget('host', 100, 2))
    first = worker.submit(_envelope())
    assert provider.request.capacity_token
    assert worker.submit(_envelope()) == first
    with pytest.raises(CapacityExceeded):
        worker.submit(_envelope(controller_job_id='second', idempotency_key='second'))


def test_capacity_toml_roundtrip_and_validation(tmp_path):
    from sonder_runtime.platform.config import load_config, ConfigError
    path = tmp_path / 'sonder.toml'
    path.write_text('[compute]\nworker_host_id="workstation"\nworker_memory_budget_bytes=4096\nworker_max_jobs=3\nworker_reservation_seconds=20\n')
    config = load_config(path, env={})
    assert config.compute.worker_memory_budget_bytes == 4096
    assert config.as_redacted_dict()['compute']['worker_host_id'] == 'workstation'
    path.write_text('[compute]\nworker_memory_budget_bytes=-1\n')
    with pytest.raises(ConfigError):
        load_config(path, env={})

def test_dispatch_rechecks_expiry_after_acquiring_database_write_lock(tmp_path):
    import sqlite3
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    now = ['2026-09-04T12:00:00+00:00']
    advance = [False]
    class Connection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            result = super().execute(statement, *args, **kwargs)
            if statement == 'BEGIN IMMEDIATE' and advance[0]:
                now[0] = '2026-09-04T12:00:02+00:00'
            return result
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db', clock=lambda: now[0],
        connect_factory=lambda *args, **kwargs: sqlite3.connect(*args, **kwargs, factory=Connection))
    lease = registry.reserve_capacity(WorkerBudget('host', 100), 'job', 'a' * 64, 100, lease_seconds=1)
    advance[0] = True
    with pytest.raises(Conflict):
        registry.dispatch_capacity('job', lease.token)


def test_preexisting_active_catalog_job_blocks_new_admissions(tmp_path):
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    from sonder_runtime.application.ports.jobs import JobIdentity
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db')
    registry.start(JobIdentity('legacy', 'compute-build', 'op', 'legacy'))
    with pytest.raises(CapacityExceeded):
        registry.reserve_capacity(WorkerBudget('host', 100), 'new', 'a' * 64, 1)

def test_dynamic_budget_requires_fresh_local_ram_and_is_exclusive():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from sonder_runtime.application.compute_fabric.capacity import measured_worker_budget
    from sonder_runtime.domain.common.errors import DependencyUnavailable
    now = datetime.now(timezone.utc)
    snapshot = SimpleNamespace(node=SimpleNamespace(local=True), observed_at=now,
        resources=SimpleNamespace(free_ram_bytes=1234))
    budget = measured_worker_budget(snapshot, 'host', now=now)
    assert budget.memory_bytes == 1234 and budget.max_jobs == 1
    for age, memory, local in [(6, 1234, True), (0, None, True), (0, 1234, False)]:
        snapshot.observed_at = now - timedelta(seconds=age)
        snapshot.resources.free_ram_bytes = memory
        snapshot.node.local = local
        with pytest.raises(DependencyUnavailable, match='fresh local'):
            measured_worker_budget(snapshot, 'host', now=now)


def test_independent_processes_share_the_durable_capacity_authority(tmp_path):
    import subprocess
    import sys
    path = tmp_path / 'jobs.db'
    SQLiteDurableJobRegistry(path)
    code = '''
import sys
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
from sonder_runtime.domain.common.errors import CapacityExceeded
r = SQLiteDurableJobRegistry(sys.argv[1])
try:
    lease = r.reserve_capacity(WorkerBudget('host', 100, 2), 'job-' + sys.argv[2], sys.argv[2] * 64, 60)
    r.dispatch_capacity(lease.job_id, lease.token)
    print('admitted')
except CapacityExceeded:
    print('occupied')
'''
    children = [subprocess.Popen([sys.executable, '-c', code, str(path), str(i)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(4)]
    results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=20)
        assert child.returncode == 0, stderr
        results.append(stdout.strip())
    assert results.count('admitted') == 1
    assert results.count('occupied') == 3

@pytest.mark.parametrize("deployment_profile", ["single-host", "pooled-pair"])
def test_production_composition_measures_and_enforces_default_exclusive_admission(tmp_path, monkeypatch, deployment_profile):
    from sonder_runtime.bootstrap import app as bootstrap
    from sonder_runtime.platform.config import SonderConfig, ComputeConfig, ComputeJobConfig, ComputeNodeConfig, DeploymentConfig
    from sonder_runtime.adapters.compute_fabric.local_snapshot import LocalComputeSnapshotSource
    from sonder_runtime.domain.compute_fabric import NodeSnapshot, NodeResources, WorkloadKind
    from sonder_runtime.application.compute_fabric.jobs import ComputeJobWorker, RemoteJobEnvelope
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from tests.test_job004_process_provider import _ScopedLimiter, _ScopedToken, _Process
    import sys
    monkeypatch.setenv('SONDER_HOME', str(tmp_path))
    monkeypatch.setenv('SONDER_JOBS_DB', str(tmp_path / 'jobs.db'))
    monkeypatch.setattr(ComputeJobWorker, '_artifact_stage_base', staticmethod(lambda: tmp_path / 'artifacts'))
    monkeypatch.setattr(LocalComputeSnapshotSource, 'snapshot', lambda self, node, now:
        NodeSnapshot(node=node, observed_at=now, resources=NodeResources(free_ram_bytes=100)))
    monkeypatch.setattr(bootstrap, 'SubprocessJobProvider', lambda registry, **kwargs:
        SubprocessJobProvider(registry, **kwargs, memory_limiter=_ScopedLimiter(_ScopedToken()),
            launcher=lambda *a, **kw: _Process(), process_identity_resolver=lambda pid: "test-process"))
    peers = (ComputeNodeConfig(node_id='primary', origin='https://primary:11435', workloads=('test',)),) if deployment_profile == 'pooled-pair' else ()
    config = SonderConfig(
        deployment=DeploymentConfig(profile=deployment_profile, preferred_primary='primary' if peers else ''),
        compute=ComputeConfig(nodes=peers, jobs=(ComputeJobConfig(job_id='test',
            workload='test', program=sys.executable, workspace_mappings=('default',)),)),
    )
    app = bootstrap.build_application(config=config)
    worker = app.compute_job_worker()
    def envelope(name):
        return RemoteJobEnvelope.create(controller_job_id=name, idempotency_key=name,
            workload=WorkloadKind.TEST, catalog_entry_id='test', workspace_mapping='default')
    first = worker.submit(envelope('first'))
    with pytest.raises(CapacityExceeded):
        worker.submit(envelope('second'))
    worker.cancel(first.remote_job_id)
    second = worker.submit(envelope('second'))
    worker.cancel(second.remote_job_id)

def test_host_identity_change_cannot_escape_occupied_authority(tmp_path):
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db')
    lease = registry.reserve_capacity(WorkerBudget('host-a', 100), 'one', 'a' * 64, 100)
    registry.dispatch_capacity('one', lease.token)
    with pytest.raises(Conflict):
        registry.reserve_capacity(WorkerBudget('host-b', 100), 'two', 'b' * 64, 100)

def test_terminal_status_alone_and_token_replay_never_release_capacity(tmp_path):
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db')
    budget = WorkerBudget('host', 100)
    lease = registry.reserve_capacity(budget, 'one', 'a' * 64, 100)
    registry.start(JobIdentity('one', 'compute-test', 'op', 'one'))
    registry.dispatch_capacity('one', lease.token)
    with pytest.raises(Conflict):
        registry.dispatch_capacity('one', lease.token)
    registry.transition('one', JobStatus.FAILED, error='unknown')
    with pytest.raises(CapacityExceeded):
        registry.reserve_capacity(budget, 'two', 'b' * 64, 100)

def test_expired_unknown_demand_renews_to_entire_current_budget(tmp_path):
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget
    now = ['2026-09-04T12:00:00+00:00']
    registry = SQLiteDurableJobRegistry(tmp_path / 'jobs.db', clock=lambda: now[0])
    registry.reserve_capacity(WorkerBudget('host', 40, 2), 'one', 'a' * 64, None, lease_seconds=1)
    now[0] = '2026-09-04T12:00:02+00:00'
    lease = registry.reserve_capacity(WorkerBudget('host', 100, 2), 'one', 'a' * 64, None)
    registry.dispatch_capacity('one', lease.token)
    with pytest.raises(CapacityExceeded):
        registry.reserve_capacity(WorkerBudget('host', 100, 2), 'two', 'b' * 64, 1)
