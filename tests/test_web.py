import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.state import RepoState, Status
from app.web import create_app


class FakeScheduler:
    def __init__(self):
        self.busy = False
        self.triggered = 0

    def is_running(self):
        return self.busy

    def trigger_now(self):
        if self.busy:
            return False
        self.triggered += 1
        return True


@pytest.fixture
def ctx(tmp_path):
    db = Database(tmp_path / "state.db")
    sched = FakeScheduler()
    client = TestClient(create_app(db, sched))
    yield db, sched, client
    db.close()


def test_index_lists_repos(ctx):
    db, _, client = ctx
    r = db.add_repo("owner/repo")
    db.save_repo_state(r.id, RepoState(Status.MISSING, 2), checked_at="t", success=False,
                       error="não encontrado no GitHub (404)", size_bytes=None)
    page = client.get("/")
    assert page.status_code == 200
    assert "owner/repo" in page.text
    assert "status-missing" in page.text
    assert "não encontrado no GitHub (404)" in page.text


def test_add_repo_normalizes(ctx):
    db, _, client = ctx
    resp = client.post("/repos", data={"full_name": "https://github.com/Owner/Repo.git"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?msg=added"
    assert [r.full_name for r in db.list_repos()] == ["owner/repo"]


def test_add_invalid_and_duplicate(ctx):
    db, _, client = ctx
    bad = client.post("/repos", data={"full_name": "nope"}, follow_redirects=False)
    assert bad.headers["location"] == "/?msg=invalid"
    client.post("/repos", data={"full_name": "owner/repo"})
    dup = client.post("/repos", data={"full_name": "owner/repo"}, follow_redirects=False)
    assert dup.headers["location"] == "/?msg=duplicate"


def test_message_rendered_and_unknown_ignored(ctx):
    _, _, client = ctx
    assert "Repositório adicionado." in client.get("/?msg=added").text
    assert "<b>x</b>" not in client.get("/?msg=<b>x</b>").text


def test_delete_repo(ctx):
    db, _, client = ctx
    r = db.add_repo("owner/repo")
    resp = client.post(f"/repos/{r.id}/delete", follow_redirects=False)
    assert resp.headers["location"] == "/?msg=removed"
    assert db.list_repos() == []


def test_sync_now(ctx):
    _, sched, client = ctx
    ok = client.post("/sync", follow_redirects=False)
    assert ok.headers["location"] == "/?msg=sync_started"
    assert sched.triggered == 1
    sched.busy = True
    busy = client.post("/sync", follow_redirects=False)
    assert busy.headers["location"] == "/?msg=sync_busy"
    assert "em execução" in client.get("/").text


def test_runs_page(ctx):
    db, _, client = ctx
    r = db.add_repo("owner/repo")
    run_id = db.start_run("manual", "2026-10-23T03:00:00+00:00")
    db.add_event(r.id, run_id, "synced", None, "2026-10-23T03:00:01+00:00")
    db.finish_run(run_id, 1, 0, "2026-10-23T03:00:02+00:00")
    page = client.get("/runs")
    assert page.status_code == 200
    assert "manual" in page.text and "synced" in page.text and "owner/repo" in page.text


def test_health(ctx):
    db, _, client = ctx
    assert client.get("/health").json() == {"ok": True, "last_run": None}
    run_id = db.start_run("manual", "a")
    db.finish_run(run_id, 0, 0, "b")
    assert client.get("/health").json() == {"ok": True, "last_run": "b"}
