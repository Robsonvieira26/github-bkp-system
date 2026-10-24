import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.db import Database, Repo
from app.github_client import CheckResult, RepoCheck
from app.mirror import GitError, ensure_mirror, mirror_path, repo_size, sync_mirror
from app.notifier import EmailMessage, render_events_email, render_summary_email
from app.state import EMAIL_EVENTS, Outcome, next_state

log = logging.getLogger(__name__)

NOT_FOUND_MSG = "não encontrado no GitHub (404)"


class RepoChecker(Protocol):
    def check_repo(self, full_name: str) -> RepoCheck: ...


class Sender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class BackupJob:
    def __init__(
        self,
        db: Database,
        github: RepoChecker,
        notifier: Sender,
        *,
        repos_dir: Path,
        git_base_url: str,
        token: str | None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._db = db
        self._github = github
        self._notifier = notifier
        self._repos_dir = repos_dir
        self._git_base_url = git_base_url.rstrip("/")
        self._token = token
        self._now = now

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def run(self, trigger: str) -> int:
        run_id = self._db.start_run(trigger, self._stamp())
        ok = fail = 0
        for repo in self._db.list_repos():
            if self._process(repo, run_id):
                ok += 1
            else:
                fail += 1
        self._db.finish_run(run_id, ok, fail, self._stamp())
        log.info("run %s finished: ok=%s fail=%s", run_id, ok, fail)
        self.flush_emails()
        return run_id

    def _process(self, repo: Repo, run_id: int) -> bool:
        stamp = self._stamp()
        size = None
        try:
            outcome, error, size = self._sync(repo, run_id, stamp)
        except Exception as exc:
            log.exception("unexpected error processing %s", repo.full_name)
            outcome, error = Outcome.ERROR, f"erro inesperado: {exc.__class__.__name__}"

        transition = next_state(repo.state, outcome)
        for kind in transition.events:
            detail = error if kind in ("error", "backup_failing", "not_found", "missing") else None
            if kind == "restored":
                since = self._db.last_event_at(repo.id, "missing")
                detail = f"marcado como sumido em {since[:10]}" if since else None
            self._db.add_event(repo.id, run_id, kind, detail, stamp)
        self._db.save_repo_state(
            repo.id,
            transition.state,
            checked_at=stamp,
            success=outcome is Outcome.OK,
            error=error,
            size_bytes=size,
        )
        log.info("%s: %s -> %s", repo.full_name, outcome.value, transition.state.status.value)
        return outcome is Outcome.OK

    def _sync(self, repo: Repo, run_id: int, stamp: str) -> tuple[Outcome, str | None, int | None]:
        check = self._github.check_repo(repo.full_name)
        if check.result is CheckResult.NOT_FOUND:
            return Outcome.NOT_FOUND, NOT_FOUND_MSG, None
        if check.result is CheckResult.ERROR:
            return Outcome.ERROR, check.detail, None

        path = mirror_path(self._repos_dir, repo.full_name)
        url = f"{self._git_base_url}/{repo.full_name}.git"
        try:
            ensure_mirror(path, url, self._token)
            preserved = sync_mirror(path, self._token, stamp[:10])
        except GitError as exc:
            # Includes timeouts and a missing git binary (see app.mirror._run).
            return Outcome.ERROR, str(exc), None
        if preserved:
            self._db.add_event(repo.id, run_id, "refs_preserved", ", ".join(preserved), stamp)
        return Outcome.OK, None, repo_size(path)

    def flush_emails(self) -> None:
        events = self._db.pending_email_events(EMAIL_EVENTS)
        if not events:
            return
        try:
            self._notifier.send(render_events_email(events, self._repos_dir))
        except Exception:
            log.exception("failed to send alert email; will retry next run")
            return
        self._db.mark_emailed(e.id for e in events)

    def send_summary(self) -> None:
        since = (self._now() - timedelta(days=7)).isoformat(timespec="seconds")
        try:
            message = render_summary_email(self._db.list_repos(), self._db.events_since(since))
            self._notifier.send(message)
        except Exception:
            log.exception("failed to send weekly summary")
