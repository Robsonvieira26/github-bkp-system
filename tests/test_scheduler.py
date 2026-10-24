import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.scheduler import Scheduler, cron_trigger


class BlockingJob:
    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.triggers = []
        self.summaries = 0

    def run(self, trigger):
        self.triggers.append(trigger)
        self.started.set()
        self.release.wait(5)
        self.finished.set()

    def send_summary(self):
        self.summaries += 1


def make(job):
    return Scheduler(job, sync_cron="0 3 * * *", summary_cron="0 9 * * 0", tz="UTC")


def test_trigger_now_runs_once_at_a_time():
    job = BlockingJob()
    s = make(job)
    assert s.trigger_now() is True
    assert job.started.wait(5)
    assert s.is_running() is True
    assert s.trigger_now() is False
    job.release.set()
    assert job.finished.wait(5)
    for _ in range(50):
        if not s.is_running():
            break
        threading.Event().wait(0.05)
    assert s.is_running() is False
    assert job.triggers == ["manual"]


def test_scheduled_run_skips_when_busy():
    job = BlockingJob()
    s = make(job)
    s.trigger_now()
    assert job.started.wait(5)
    s._scheduled_sync()  # returns immediately, does not block or run
    assert job.triggers == ["manual"]
    job.release.set()


def test_scheduled_run_uses_schedule_trigger():
    job = BlockingJob()
    job.release.set()
    s = make(job)
    s._scheduled_sync()
    assert job.triggers == ["schedule"]
    assert s.is_running() is False


def test_jobs_registered():
    s = make(BlockingJob())
    assert {j.id for j in s.jobs()} == {"sync", "summary"}


def test_invalid_cron_rejected():
    with pytest.raises(ValueError):
        Scheduler(BlockingJob(), sync_cron="not a cron", summary_cron="0 9 * * 0", tz="UTC")


def _fires(expr, start, n=7):
    trigger = cron_trigger(expr, "UTC")
    out, prev, now = [], None, start
    for _ in range(n):
        prev = trigger.get_next_fire_time(prev, now)
        out.append(prev)
        now = prev
    return out


def test_default_summary_cron_fires_on_sunday_0900():
    start = datetime(2026, 10, 23, tzinfo=UTC)
    nxt = cron_trigger("0 9 * * 0", "UTC").get_next_fire_time(None, start)
    assert nxt.weekday() == 6
    assert (nxt.hour, nxt.minute) == (9, 0)


def test_day_of_week_7_is_sunday():
    nxt = cron_trigger("0 9 * * 7", "UTC").get_next_fire_time(
        None, datetime(2026, 10, 23, tzinfo=UTC)
    )
    assert nxt.weekday() == 6


def test_day_of_week_range_is_monday_to_friday():
    days = {d.weekday() for d in _fires("0 9 * * 1-5", datetime(2026, 10, 23, tzinfo=UTC), 10)}
    assert days == {0, 1, 2, 3, 4}


def test_day_of_week_list_and_names():
    days = {d.weekday() for d in _fires("0 9 * * 1,3,5", datetime(2026, 10, 23, tzinfo=UTC), 6)}
    assert days == {0, 2, 4}
    days = {d.weekday() for d in _fires("0 9 * * mon-fri", datetime(2026, 10, 23, tzinfo=UTC), 10)}
    assert days == {0, 1, 2, 3, 4}


@pytest.mark.parametrize("expr", ["0 9 * *", "0 9 * * * *", "", "a b c d e"])
def test_cron_trigger_invalid_raises_value_error(expr):
    with pytest.raises(ValueError):
        cron_trigger(expr, "UTC")


NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def make_catchup(job, last):
    return Scheduler(
        job,
        sync_cron="0 3 * * *",
        summary_cron="0 9 * * 0",
        tz="UTC",
        last_run_started=lambda: last,
        now=lambda: NOW,
    )


def _wait_idle(s):
    for _ in range(100):
        if not s.is_running():
            return
        threading.Event().wait(0.05)


@pytest.mark.parametrize("last", [None, (NOW - timedelta(hours=24)).isoformat()])
def test_start_catches_up_when_last_run_is_stale(last):
    job = BlockingJob()
    job.release.set()
    s = make_catchup(job, last)
    s.start()
    try:
        assert job.finished.wait(5)
        assert job.triggers == ["startup"]
    finally:
        s.shutdown()
        _wait_idle(s)


def test_start_skips_catch_up_when_last_run_is_recent():
    job = BlockingJob()
    s = make_catchup(job, (NOW - timedelta(hours=2)).isoformat())
    s.start()
    try:
        assert s.is_running() is False
        assert job.triggers == []
    finally:
        s.shutdown()


def test_jobs_have_misfire_grace_time():
    s = make(BlockingJob())
    assert {j.misfire_grace_time for j in s.jobs()} == {3600}
