import logging
import threading
from typing import Protocol

from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)


class RunnableJob(Protocol):
    def run(self, trigger: str) -> int: ...

    def send_summary(self) -> None: ...


class Scheduler:
    def __init__(self, job: RunnableJob, *, sync_cron: str, summary_cron: str, tz: str):
        self._job = job
        self._lock = threading.Lock()
        self._sched = BackgroundScheduler(timezone=tz)
        self._sched.add_job(
            self._scheduled_sync,
            CronTrigger.from_crontab(sync_cron, timezone=tz),
            id="sync",
            max_instances=1,
            coalesce=True,
        )
        self._sched.add_job(
            job.send_summary,
            CronTrigger.from_crontab(summary_cron, timezone=tz),
            id="summary",
            max_instances=1,
            coalesce=True,
        )

    def start(self) -> None:
        self._sched.start()

    def shutdown(self) -> None:
        self._sched.shutdown(wait=False)

    def jobs(self) -> list[Job]:
        return self._sched.get_jobs()

    def is_running(self) -> bool:
        return self._lock.locked()

    def trigger_now(self) -> bool:
        """Start a manual run in the background. False if a run is already in progress."""
        if not self._lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._run_locked, args=("manual",), daemon=True).start()
        return True

    def _scheduled_sync(self) -> None:
        if not self._lock.acquire(blocking=False):
            log.warning("scheduled sync skipped: a run is already in progress")
            return
        self._run_locked("schedule")

    def _run_locked(self, trigger: str) -> None:
        try:
            self._job.run(trigger)
        except Exception:
            log.exception("backup run failed")
        finally:
            self._lock.release()
