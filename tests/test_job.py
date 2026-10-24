import smtplib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.db import Database
from app.github_client import CheckResult, RepoCheck
from app.job import BackupJob
from app.mirror import mirror_path
from app.state import Status
from tests.gitutil import git, has_ref


class FakeGitHub:
    def __init__(self):
        self.results: dict[str, RepoCheck] = {}

    def check_repo(self, full_name):
        return self.results.get(full_name, RepoCheck(CheckResult.EXISTS))


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, message):
        if self.fail:
            raise smtplib.SMTPException("down")
        self.sent.append(message)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def next_day(self):
        self.now += timedelta(days=1)


@pytest.fixture
def env(tmp_path, remote):
    db = Database(tmp_path / "state.db")
    gh, nt, clock = FakeGitHub(), FakeNotifier(), Clock()
    repos_dir = tmp_path / "repos"
    job = BackupJob(
        db, gh, nt, repos_dir=repos_dir, git_base_url=remote.base_url, token=None, now=clock
    )
    yield SimpleNamespace(
        db=db, gh=gh, nt=nt, clock=clock, job=job, remote=remote, repos_dir=repos_dir
    )
    db.close()


def run_days(env, n):
    for _ in range(n):
        env.job.run("schedule")
        env.clock.next_day()


def kinds(env, run_id):
    return [e.kind for e in env.db.events_for_run(run_id)]


def test_new_repo_is_mirrored(env):
    r = env.db.add_repo("owner/repo")
    run_id = env.job.run("manual")
    got = env.db.get_repo(r.id)
    assert got.status is Status.ACTIVE
    assert got.last_success_at == "2026-10-23T03:00:00+00:00"
    assert got.size_bytes > 0
    assert has_ref(mirror_path(env.repos_dir, "owner/repo"), "refs/heads/main")
    assert kinds(env, run_id) == ["synced"]
    run = env.db.last_run()
    assert (run.trigger, run.ok_count, run.fail_count) == ("manual", 1, 0)
    assert env.nt.sent == []


def test_missing_after_two_not_found(env):
    r = env.db.add_repo("owner/repo")
    run_days(env, 1)
    env.gh.results["owner/repo"] = RepoCheck(CheckResult.NOT_FOUND)

    env.job.run("schedule")
    assert env.db.get_repo(r.id).status is Status.SUSPECT
    assert env.nt.sent == []

    env.clock.next_day()
    env.job.run("schedule")
    assert env.db.get_repo(r.id).status is Status.MISSING
    assert len(env.nt.sent) == 1
    assert "sumiu" in env.nt.sent[0].subject
    assert "owner/repo" in env.nt.sent[0].text

    env.clock.next_day()
    env.job.run("schedule")
    assert len(env.nt.sent) == 1  # no repeat


def test_restored_sends_email(env):
    r = env.db.add_repo("owner/repo")
    env.gh.results["owner/repo"] = RepoCheck(CheckResult.NOT_FOUND)
    run_days(env, 2)
    del env.gh.results["owner/repo"]
    env.job.run("schedule")
    assert env.db.get_repo(r.id).status is Status.ACTIVE
    assert len(env.nt.sent) == 2
    assert "owner/repo voltou" in env.nt.sent[1].text
    assert "fora por 1 dia(s), desde 2026-10-24" in env.nt.sent[1].text


def test_backup_failing_after_three_errors(env):
    r = env.db.add_repo("owner/repo")
    env.gh.results["owner/repo"] = RepoCheck(CheckResult.ERROR, "HTTP 500")
    run_days(env, 2)
    assert env.nt.sent == []
    run_days(env, 1)
    assert len(env.nt.sent) == 1
    assert "HTTP 500" in env.nt.sent[0].text
    run_days(env, 1)
    assert len(env.nt.sent) == 1
    assert env.db.get_repo(r.id).last_error == "HTTP 500"


def test_git_failure_is_error(env):
    r = env.db.add_repo("owner/does-not-exist-locally")
    env.job.run("schedule")
    got = env.db.get_repo(r.id)
    assert got.status is Status.ERROR
    assert got.last_error.startswith("git clone falhou")
    assert env.db.last_run().fail_count == 1


def test_events_from_one_run_grouped(env):
    env.db.add_repo("owner/a")
    env.db.add_repo("owner/b")
    env.gh.results["owner/a"] = RepoCheck(CheckResult.NOT_FOUND)
    env.gh.results["owner/b"] = RepoCheck(CheckResult.NOT_FOUND)
    run_days(env, 2)
    assert len(env.nt.sent) == 1
    assert "owner/a" in env.nt.sent[0].text and "owner/b" in env.nt.sent[0].text


def test_failed_email_is_retried(env):
    env.db.add_repo("owner/repo")
    env.gh.results["owner/repo"] = RepoCheck(CheckResult.NOT_FOUND)
    env.nt.fail = True
    run_days(env, 2)
    assert env.nt.sent == []
    env.nt.fail = False
    env.job.run("schedule")
    assert len(env.nt.sent) == 1
    assert "owner/repo" in env.nt.sent[0].text


def test_refs_preserved_event(env):
    r = env.db.add_repo("owner/repo")
    git("branch", "gone", cwd=env.remote.work)
    env.remote.push("gone")
    run_days(env, 1)
    env.remote.push("--delete", "gone")
    run_id = env.job.run("schedule")
    events = env.db.events_for_run(run_id)
    assert [e.kind for e in events] == ["refs_preserved", "synced"]
    assert events[0].detail == "refs/heads/gone"
    assert env.db.get_repo(r.id).status is Status.ACTIVE


def test_unexpected_exception_does_not_stop_run(env, monkeypatch):
    env.db.add_repo("owner/a")
    env.db.add_repo("owner/repo")

    real = env.gh.check_repo

    def flaky(name):
        if name == "owner/a":
            raise RuntimeError("bug")
        return real(name)

    monkeypatch.setattr(env.gh, "check_repo", flaky)
    env.job.run("schedule")
    statuses = {r.full_name: r.status for r in env.db.list_repos()}
    assert statuses == {"owner/a": Status.ERROR, "owner/repo": Status.ACTIVE}


def test_send_summary(env):
    env.db.add_repo("owner/repo")
    env.job.run("schedule")
    env.job.send_summary()
    assert len(env.nt.sent) == 1
    assert env.nt.sent[0].subject.startswith("[github-bkp] Resumo semanal")
    assert "synced: 1" in env.nt.sent[0].text


def test_db_error_on_one_repo_does_not_abort_run(env, monkeypatch):
    bad = env.db.add_repo("owner/bad")
    good = env.db.add_repo("owner/repo")
    real_save = env.db.save_repo_state

    def flaky_save(repo_id, *args, **kwargs):
        if repo_id == bad.id:
            raise RuntimeError("disk I/O error")
        return real_save(repo_id, *args, **kwargs)

    monkeypatch.setattr(env.db, "save_repo_state", flaky_save)
    env.job.run("manual")
    assert env.db.get_repo(good.id).status is Status.ACTIVE
    run = env.db.last_run()
    assert run.finished_at is not None
    assert (run.ok_count, run.fail_count) == (1, 1)
