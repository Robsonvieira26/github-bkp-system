import threading

import pytest

from app.scheduler import Scheduler


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
