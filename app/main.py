import json
import logging
import shutil

import uvicorn
from fastapi import FastAPI

from app.config import Settings
from app.db import Database, utc_now
from app.github_client import GitHubClient
from app.job import BackupJob
from app.notifier import Notifier
from app.scheduler import Scheduler
from app.web import create_app


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def check_data_dir(settings: Settings) -> None:
    """Fail at startup, with the fix, if mirrors can't be written (every backup would fail)."""
    try:
        settings.repos_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.repos_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"sem permissão para gravar em {settings.repos_dir} ({exc.strerror}). O container "
            f"roda como UID 1000; corrija o dono do volume com: docker run --rm --user 0 "
            f"-v github-bkp-data:/data --entrypoint chown github-bkp-system:latest "
            f"-R 1000:1000 /data"
        ) from exc


def build(settings: Settings) -> tuple[FastAPI, Scheduler]:
    if shutil.which("git") is None:
        raise RuntimeError("git não encontrado no PATH")
    check_data_dir(settings)
    db = Database(settings.db_path)
    token = settings.github_token.get_secret_value()
    job = BackupJob(
        db,
        GitHubClient(token, settings.github_api_url),
        Notifier(settings),
        repos_dir=settings.repos_dir,
        git_base_url=settings.github_git_url,
        token=token,
    )
    db.close_unfinished_runs(utc_now())
    scheduler = Scheduler(
        job,
        sync_cron=settings.sync_cron,
        summary_cron=settings.summary_cron,
        tz=settings.tz,
        last_run_started=lambda: (r.started_at if (r := db.last_run()) else None),
    )
    return create_app(db, scheduler, settings.allowed_hosts_list, tz=settings.tz), scheduler


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    settings = Settings()
    app, scheduler = build(settings)
    scheduler.start()
    try:
        uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
