from datetime import datetime

from sonder_runtime.application.health_alerts import AlertPolicy, HealthAlertService
from sonder_runtime.application.ports.health_alerts import (
    LOG_CHANNEL,
    DeliveryResult,
    TaskUnavailable,
)
from sonder_runtime.domain.health_alerts import (
    ScheduledTaskState,
    WatchedTask,
)


class FakeNotifier:
    def __init__(self, deliver=True):
        self.deliver = deliver
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)
        return (DeliveryResult("fake", self.deliver, "" if self.deliver else "down"),)


class MemoryStore:
    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.saves = 0

    def load(self):
        return dict(self.data)

    def save(self, sent):
        self.data = dict(sent)
        self.saves += 1


class FakeClock:
    def __init__(self):
        self.local = datetime(2026, 11, 15, 12, 0, 0)
        self.t = 1_000_000.0

    def now_local(self):
        return self.local

    def epoch(self):
        return self.t


class FakeTasks:
    def __init__(self, states):
        self.states = states
        self.reads = []

    def read(self, name):
        self.reads.append(name)
        item = self.states[name]
        if isinstance(item, Exception):
            raise item
        return item


FAIL_REPORT = {"checks": [{"name": "ollama", "status": "fail", "detail": "refused"}]}


def make(policy=None, deliver=True, store=None):
    notifier = FakeNotifier(deliver)
    store = store or MemoryStore()
    clock = FakeClock()
    policy = policy or AlertPolicy(rate_limit_seconds=3600)
    return HealthAlertService(notifier, store, clock, policy), notifier, store, clock


def test_failed_check_is_sent_once_then_rate_limited():
    service, notifier, store, clock = make()
    first = service.check(FAIL_REPORT, None)
    assert [a.subject for a in first.sent] == ["ollama"]
    clock.t += 60
    second = service.check(FAIL_REPORT, None)
    assert second.sent == () and [a.subject for a in second.suppressed] == ["ollama"]
    assert len(notifier.sent) == 1


def test_alert_repeats_after_the_period():
    service, notifier, store, clock = make()
    service.check(FAIL_REPORT, None)
    clock.t += 3601
    assert len(service.check(FAIL_REPORT, None).sent) == 1
    assert len(notifier.sent) == 2


def test_rate_limit_state_survives_a_new_service_instance():
    store = MemoryStore()
    service, notifier, _, clock = make(store=store)
    service.check(FAIL_REPORT, None)
    other, other_notifier, _, other_clock = make(store=store)
    other_clock.t = clock.t + 10
    other.check(FAIL_REPORT, None)
    assert other_notifier.sent == []


def test_different_problems_are_limited_independently():
    service, notifier, store, clock = make()
    service.check(FAIL_REPORT, None)
    both = {"checks": FAIL_REPORT["checks"] + [{"name": "backup", "status": "fail", "detail": "old"}]}
    run = service.check(both, None)
    assert [a.subject for a in run.sent] == ["backup"]


def test_known_off_doctor_check_never_alerts():
    service, notifier, store, clock = make(AlertPolicy(3600, known_off=("Ollama",)))
    run = service.check(FAIL_REPORT, None)
    assert notifier.sent == [] and [a.subject for a in run.known_off] == ["ollama"]
    assert store.saves == 0


def test_known_off_job_is_not_even_queried():
    tasks = FakeTasks({})
    service, notifier, *_ = make(AlertPolicy(
        3600, known_off=("SonderNightly",), watched_tasks=(WatchedTask("SonderNightly", 26),)))
    run = service.check(None, tasks)
    assert tasks.reads == [] and notifier.sent == [] and run.problems == 0


def test_missed_job_alerts_and_healthy_job_does_not():
    tasks = FakeTasks({
        "Late": ScheduledTaskState("Late", "Ready", datetime(2026, 11, 10, 3, 0), 0),
        "Fine": ScheduledTaskState("Fine", "Ready", datetime(2026, 11, 15, 9, 0), 0),
    })
    service, notifier, *_ = make(AlertPolicy(
        3600, watched_tasks=(WatchedTask("Late", 26), WatchedTask("Fine", 26))))
    run = service.check(None, tasks)
    assert [a.subject for a in run.sent] == ["Late"]
    assert notifier.sent[0].key == "task_missed:late"


def test_unreadable_job_alerts_instead_of_staying_silent():
    tasks = FakeTasks({"Gone": TaskUnavailable("schtasks exit 1")})
    service, notifier, *_ = make(AlertPolicy(3600, watched_tasks=(WatchedTask("Gone", 26),)))
    run = service.check(None, tasks)
    assert [a.key for a in run.sent] == ["task_unreadable:gone"]


def test_undelivered_alert_is_not_rate_limited_so_it_retries():
    service, notifier, store, clock = make(deliver=False)
    run = service.check(FAIL_REPORT, None)
    assert run.sent == () and [a.subject for a in run.undelivered] == ["ollama"]
    assert store.saves == 0
    clock.t += 1
    service.check(FAIL_REPORT, None)
    assert len(notifier.sent) == 2


def test_duplicate_alerts_in_one_pass_collapse():
    service, notifier, *_ = make()
    report = {"checks": FAIL_REPORT["checks"] * 2}
    service.check(report, None)
    assert len(notifier.sent) == 1


def test_no_problems_sends_nothing_and_writes_nothing():
    service, notifier, store, _ = make()
    run = service.check({"checks": [{"name": "x", "status": "ok", "detail": ""}]}, FakeTasks({}))
    assert run.problems == 0 and notifier.sent == [] and store.saves == 0


def test_future_last_run_alerts_as_unreadable_instead_of_staying_silent():
    tasks = FakeTasks({"Odd": ScheduledTaskState("Odd", "Ready", datetime(2027, 12, 1, 3, 0), 0)})
    service, notifier, *_ = make(AlertPolicy(3600, watched_tasks=(WatchedTask("Odd", 26),)))
    run = service.check(None, tasks)
    assert [a.key for a in run.sent] == ["task_unreadable:odd"]


def test_log_only_delivery_is_not_stamped_so_a_failed_channel_retries():
    class LogOnly:
        def __init__(self):
            self.sent = []

        def send(self, alert):
            self.sent.append(alert)
            return (DeliveryResult("ntfy", False, "http 500"), DeliveryResult(LOG_CHANNEL, True))

    notifier = LogOnly()
    store, clock = MemoryStore(), FakeClock()
    service = HealthAlertService(notifier, store, clock, AlertPolicy(rate_limit_seconds=3600))
    run = service.check(FAIL_REPORT, None)
    assert run.sent == () and [a.subject for a in run.undelivered] == ["ollama"]
    assert store.saves == 0 and store.data == {}
    clock.t += 1
    service.check(FAIL_REPORT, None)
    assert len(notifier.sent) == 2


def test_real_channel_delivery_alongside_log_is_stamped():
    class Both:
        def send(self, alert):
            return (DeliveryResult("toast", True), DeliveryResult(LOG_CHANNEL, True))

    store = MemoryStore()
    service = HealthAlertService(Both(), store, FakeClock(), AlertPolicy(rate_limit_seconds=3600))
    run = service.check(FAIL_REPORT, None)
    assert [a.subject for a in run.sent] == ["ollama"] and store.saves == 1
