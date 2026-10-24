import json
import logging
import shutil

import uvicorn
from fastapi import FastAPI

from app.config import Settings
from app.db import Database
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


def build(settings: Settings) -> tuple[FastAPI, Scheduler]:
    if shutil.which("git") is None:
        raise RuntimeError("git não encontrado no PATH")
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
    scheduler = Scheduler(
        job,
        sync_cron=settings.sync_cron,
        summary_cron=settings.summary_cron,
        tz=settings.tz,
        last_run_started=lambda: (r.started_at if (r := db.last_run()) else None),
    )
    return create_app(db, scheduler), scheduler


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
