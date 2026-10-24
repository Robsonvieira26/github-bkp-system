import logging
import threading
from typing import Protocol

from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

_DOW_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _dow_token(tok: str) -> str:
    if not tok.isdigit():
        return tok
    n = int(tok)
    if n > 7:
        raise ValueError(f"invalid day of week: {tok}")
    return _DOW_NAMES[(n - 1) % 7]


def _convert_dow(field: str) -> str:
    # APScheduler numbers weekdays from Monday=0; standard cron has Sunday=0/7.
    # Convert numeric tokens to names so users can write standard cron.
    parts = []
    for item in field.split(","):
        base, slash, step = item.partition("/")
        base = "-".join(_dow_token(t) for t in base.split("-"))
        parts.append(base + slash + step)
    return ",".join(parts)


def cron_trigger(expr: str, tz: str) -> CronTrigger:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expr!r}")
    minute, hour, day, month, dow = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=_convert_dow(dow),
        timezone=tz,
    )


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
            cron_trigger(sync_cron, tz),
            id="sync",
            max_instances=1,
            coalesce=True,
        )
        self._sched.add_job(
            job.send_summary,
            cron_trigger(summary_cron, tz),
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
