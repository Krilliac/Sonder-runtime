from datetime import datetime

import pytest

from sonder_runtime.domain.health_alerts import (
    AlertKind,
    DateOrder,
    ScheduledTaskState,
    TaskParseError,
    alerts_from_doctor_report,
    is_known_off,
    parse_schtasks_csv,
    parse_watched_task,
    prune_sent,
    rate_limit_allows,
    task_alert,
)

NOW = datetime(2026, 11, 15, 12, 0, 0)

HEADER = (
    '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time",'
    '"Last Result","Author","Task To Run","Scheduled Task State"'
)


def csv_row(name, last_run, result, state="Ready"):
    return '%s\n"HOST","\\%s","N/A","Ready","Interactive only","%s","%s","me","cmd","%s"\n' % (
        HEADER, name, last_run, result, state)


def test_doctor_fail_becomes_alert_and_ok_does_not():
    report = {"overall": "fail", "checks": [
        {"name": "ollama", "status": "fail", "detail": "refused"},
        {"name": "config", "status": "ok", "detail": ""},
        {"name": "backup", "status": "warn", "detail": "old"},
        {"name": "embeddings", "status": "skipped", "detail": ""},
    ]}
    alerts = alerts_from_doctor_report(report)
    assert [a.subject for a in alerts] == ["ollama"]
    assert alerts[0].kind is AlertKind.DOCTOR_CHECK
    assert alerts[0].key == "doctor_check:ollama"
    assert "refused" in alerts[0].detail


def test_doctor_warn_alerts_only_when_asked():
    report = {"checks": [{"name": "backup", "status": "warn", "detail": "old"}]}
    assert alerts_from_doctor_report(report) == ()
    assert [a.subject for a in alerts_from_doctor_report(report, include_warn=True)] == ["backup"]


@pytest.mark.parametrize("report", [{}, {"checks": None}, {"checks": [1, "x"]}])
def test_doctor_report_of_unexpected_shape_yields_no_alerts(report):
    assert alerts_from_doctor_report(report) == ()


def test_known_off_is_case_insensitive():
    assert is_known_off("SonderNightly", ["sondernightly"])
    assert not is_known_off("Other", ["sondernightly"])


def test_parse_schtasks_csv_reads_last_run_and_result():
    state = parse_schtasks_csv(csv_row("JobA", "11/14/2026 9:15:01 PM", "-2147024629"), "JobA")
    assert state.last_run == datetime(2026, 11, 14, 21, 15, 1)
    assert state.last_result == -2147024629
    assert state.scheduler_state == "Ready"


def test_parse_schtasks_csv_never_run_placeholder():
    state = parse_schtasks_csv(csv_row("JobA", "11/30/1999 12:00:00 AM", "267011"), "JobA")
    assert state.last_run is None
    assert state.last_result == 267011


@pytest.mark.parametrize("text", ["", HEADER + "\n"])
def test_parse_schtasks_csv_without_rows_is_typed_error(text):
    with pytest.raises(TaskParseError):
        parse_schtasks_csv(text, "JobA")


def test_parse_schtasks_csv_missing_column_is_typed_error():
    with pytest.raises(TaskParseError):
        parse_schtasks_csv('"TaskName","Other"\n"\\JobA","x"\n', "JobA")


def test_parse_schtasks_csv_unrecognised_time_is_typed_error():
    with pytest.raises(TaskParseError):
        parse_schtasks_csv(csv_row("JobA", "yesterday-ish", "0"), "JobA")


def state(last_run, result=0):
    return ScheduledTaskState("JobA", "Ready", last_run, result)


def test_task_inside_window_and_ok_is_quiet():
    assert task_alert(state(datetime(2026, 11, 15, 3, 0)), 26, NOW) is None


def test_task_still_running_is_quiet():
    assert task_alert(state(datetime(2026, 11, 15, 3, 0), 267009), 26, NOW) is None


def test_task_past_window_is_missed():
    alert = task_alert(state(datetime(2026, 11, 13, 3, 0)), 26, NOW)
    assert alert.kind is AlertKind.TASK_MISSED
    assert alert.key == "task_missed:joba"


def test_task_never_run_is_missed():
    assert task_alert(state(None, 267011), 26, NOW).kind is AlertKind.TASK_MISSED
    assert task_alert(state(None, None), 26, NOW).kind is AlertKind.TASK_MISSED


def test_task_inside_window_with_failure_result_is_failed():
    alert = task_alert(state(datetime(2026, 11, 15, 3, 0), -2147024629), 26, NOW)
    assert alert.kind is AlertKind.TASK_FAILED
    assert "-2147024629" in alert.detail


def test_parse_watched_task_forms():
    assert parse_watched_task("A", default_window_hours=26).window_hours == 26
    watched = parse_watched_task("Folder\\B=5.5", default_window_hours=26)
    assert (watched.name, watched.window_hours) == ("Folder\\B", 5.5)


@pytest.mark.parametrize("entry", ["", "=5", "A=x", "A=0", "A=-1", "A=99999"])
def test_parse_watched_task_rejects_bad_entries(entry):
    with pytest.raises(ValueError):
        parse_watched_task(entry, default_window_hours=26)


def test_rate_limit_allows_first_and_after_period_only():
    assert rate_limit_allows(None, 1000, 60)
    assert not rate_limit_allows(990, 1000, 60)
    assert rate_limit_allows(900, 1000, 60)


def test_rate_limit_allows_when_clock_went_backwards():
    assert rate_limit_allows(5000, 1000, 60)


def test_prune_drops_only_entries_too_old_to_matter():
    kept = prune_sent({"old": 0.0, "recent": 990.0}, 1000.0, 10.0)
    assert kept == {"recent": 990.0}


def test_day_first_date_is_read_correctly_under_dmy_and_alerts_when_late():
    text = csv_row("JobA", "12/01/2027 03:00:00", "0")
    parsed = parse_schtasks_csv(text, "JobA", date_order=DateOrder.DMY)
    assert parsed.last_run == datetime(2027, 1, 12, 3, 0, 0)
    alert = task_alert(parsed, 26, datetime(2027, 1, 14, 12, 0, 0))
    assert alert.kind is AlertKind.TASK_MISSED
    assert task_alert(parsed, 26, datetime(2027, 1, 12, 12, 0, 0)) is None


def test_same_text_under_month_first_hint_is_a_different_date():
    parsed = parse_schtasks_csv(csv_row("JobA", "12/01/2027 03:00:00", "0"), "JobA",
                                date_order=DateOrder.MDY)
    assert parsed.last_run == datetime(2027, 12, 1, 3, 0, 0)


@pytest.mark.parametrize("text", ["12/01/2027 03:00:00", "12/01/2027 3:00:00 AM"])
def test_ambiguous_date_with_unknown_order_is_refused(text):
    with pytest.raises(TaskParseError):
        parse_schtasks_csv(csv_row("JobA", text, "0"), "JobA")
    with pytest.raises(TaskParseError):
        parse_schtasks_csv(csv_row("JobA", text, "0"), "JobA", date_order=DateOrder.UNKNOWN)


@pytest.mark.parametrize("text", ["13/01/2027 03:00:00", "05/05/2027 03:00:00", "2027-01-12 03:00:00"])
def test_unambiguous_dates_parse_even_with_unknown_order(text):
    assert parse_schtasks_csv(csv_row("JobA", text, "0"), "JobA").last_run is not None


def test_year_first_text_is_read_under_every_order():
    for order in DateOrder:
        parsed = parse_schtasks_csv(csv_row("JobA", "2027-01-12 03:00:00", "0"), "JobA",
                                    date_order=order)
        assert parsed.last_run == datetime(2027, 1, 12, 3, 0, 0)


def test_dotted_date_needs_day_first_or_unknown_order():
    text = csv_row("JobA", "12.01.2027 03:00:00", "0")
    assert parse_schtasks_csv(text, "JobA", date_order=DateOrder.DMY).last_run == datetime(
        2027, 1, 12, 3, 0, 0)
    with pytest.raises(TaskParseError):
        parse_schtasks_csv(text, "JobA", date_order=DateOrder.MDY)


def test_future_last_run_is_refused_not_healthy():
    with pytest.raises(TaskParseError):
        task_alert(state(datetime(2026, 11, 15, 12, 6, 0)), 26, NOW)
    with pytest.raises(TaskParseError):
        task_alert(state(datetime(2027, 12, 1, 3, 0, 0)), 26, NOW)


def test_small_clock_skew_into_the_future_is_tolerated():
    assert task_alert(state(datetime(2026, 11, 15, 12, 4, 0)), 26, NOW) is None
