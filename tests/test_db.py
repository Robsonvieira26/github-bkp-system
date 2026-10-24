import pytest

from app.db import Database, DuplicateRepoError
from app.state import RepoState, Status

T1 = "2026-10-20T03:00:00+00:00"
T2 = "2026-10-21T03:00:00+00:00"
T3 = "2026-10-22T03:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "sub" / "state.db")
    yield d
    d.close()


def test_add_and_list(db):
    r = db.add_repo("owner/b", now=T1)
    db.add_repo("owner/a", now=T1)
    assert r.status is Status.PENDING
    assert r.enabled is True
    assert r.created_at == T1
    assert [x.full_name for x in db.list_repos()] == ["owner/a", "owner/b"]


def test_duplicate_rejected(db):
    db.add_repo("owner/a")
    with pytest.raises(DuplicateRepoError):
        db.add_repo("owner/a")


def test_disable_and_readd_keeps_history(db):
    r = db.add_repo("owner/a")
    db.save_repo_state(
        r.id, RepoState(Status.ACTIVE), checked_at=T1, success=True, error=None, size_bytes=10
    )
    assert db.disable_repo(r.id) is True
    assert db.list_repos() == []
    again = db.add_repo("owner/a")
    assert again.id == r.id
    assert again.status is Status.ACTIVE
    assert again.last_success_at == T1


def test_save_repo_state(db):
    r = db.add_repo("owner/a")
    db.save_repo_state(
        r.id, RepoState(Status.ACTIVE), checked_at=T1, success=True, error=None, size_bytes=100
    )
    db.save_repo_state(
        r.id,
        RepoState(Status.ERROR, 0, 1),
        checked_at=T2,
        success=False,
        error="boom",
        size_bytes=None,
    )
    got = db.get_repo(r.id)
    assert got.state == RepoState(Status.ERROR, 0, 1)
    assert got.last_success_at == T1
    assert got.last_checked_at == T2
    assert got.last_error == "boom"
    assert got.size_bytes == 100


def test_runs(db):
    assert db.last_run() is None
    run_id = db.start_run("manual", T1)
    db.finish_run(run_id, ok=2, fail=1, now=T2)
    last = db.last_run()
    assert (last.id, last.trigger, last.ok_count, last.fail_count) == (run_id, "manual", 2, 1)
    assert last.finished_at == T2
    db.start_run("schedule", T3)
    assert [r.trigger for r in db.recent_runs(10)] == ["schedule", "manual"]


def test_events_and_email_queue(db):
    r = db.add_repo("owner/a")
    run_id = db.start_run("schedule", T1)
    db.add_event(r.id, run_id, "synced", None, T1)
    e_missing = db.add_event(r.id, run_id, "missing", "404", T2)
    db.add_event(r.id, run_id, "restored", None, T3)

    pending = db.pending_email_events({"missing", "restored"})
    assert [e.kind for e in pending] == ["missing", "restored"]
    assert pending[0].full_name == "owner/a"
    assert pending[0].detail == "404"

    db.mark_emailed([e_missing])
    assert [e.kind for e in db.pending_email_events({"missing", "restored"})] == ["restored"]

    assert [e.kind for e in db.events_for_run(run_id)] == ["synced", "missing", "restored"]
    assert [e.kind for e in db.events_since(T2)] == ["missing", "restored"]
    assert db.last_event_at(r.id, "missing") == T2
    assert db.last_event_at(r.id, "backup_failing") is None
