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

    def next_sync_time(self):
        return None


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


def test_add_empty_name_is_invalid_not_422(ctx):
    _, _, client = ctx
    resp = client.post("/repos", data={"full_name": ""}, follow_redirects=False)
    assert resp.headers["location"] == "/?msg=invalid"
    resp = client.post("/repos", data={}, follow_redirects=False)
    assert resp.headers["location"] == "/?msg=invalid"


def test_delete_unknown_repo_not_found(ctx):
    db, _, client = ctx
    resp = client.post("/repos/999/delete", follow_redirects=False)
    assert resp.headers["location"] == "/?msg=not_found"
    assert "Repositório não encontrado." in client.get("/?msg=not_found").text


def test_delete_records_removed_event(ctx):
    db, _, client = ctx
    r = db.add_repo("owner/repo")
    client.post(f"/repos/{r.id}/delete")
    events = db.events_since("1970-01-01")
    assert [(e.repo_id, e.kind) for e in events] == [(r.id, "removed")]


def test_confirm_does_not_interpolate_name_into_js(ctx):
    db, _, client = ctx
    db.add_repo("owner/repo")
    html = client.get("/").text
    assert 'data-name="owner/repo"' in html
    assert "confirm('Remover ' + this.dataset.name" in html


def test_cross_origin_post_blocked(ctx):
    db, sched, client = ctx
    r = db.add_repo("owner/repo")
    evil = {"Origin": "http://evil.example"}
    for path, data in [("/repos", {"full_name": "a/b"}), (f"/repos/{r.id}/delete", None),
                       ("/sync", None)]:
        resp = client.post(path, data=data, headers=evil, follow_redirects=False)
        assert resp.status_code == 403
        assert resp.text == "cross-site request blocked"
    assert [x.full_name for x in db.list_repos()] == ["owner/repo"]
    assert sched.triggered == 0


def test_cross_site_fetch_metadata_blocked(ctx):
    db, _, client = ctx
    resp = client.post("/repos", data={"full_name": "a/b"},
                       headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert resp.status_code == 403
    resp = client.post("/repos", data={"full_name": "a/b"},
                       headers={"Sec-Fetch-Site": "same-site"}, follow_redirects=False)
    assert resp.status_code == 403
    assert db.list_repos() == []


def test_same_origin_post_allowed(ctx):
    db, _, client = ctx
    headers = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    resp = client.post("/repos", data={"full_name": "a/b"}, headers=headers,
                       follow_redirects=False)
    assert resp.status_code == 303
    assert [r.full_name for r in db.list_repos()] == ["a/b"]


def test_get_with_foreign_origin_still_allowed(ctx):
    _, _, client = ctx
    resp = client.get("/health", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 200


def test_untrusted_host_rejected(tmp_path):
    db = Database(tmp_path / "state.db")
    client = TestClient(create_app(db, FakeScheduler(), allowed_hosts=["127.0.0.1"]))
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/health", headers={"Host": "127.0.0.1:8080"}).status_code == 200
    db.close()


def test_runs_page_shows_interrupted(ctx):
    db, _, client = ctx
    db.start_run("manual", "2026-10-23T03:00:00+00:00")
    db.close_unfinished_runs("2026-10-23T04:00:00+00:00")
    page = client.get("/runs").text
    assert "interrompida" in page
    assert "em execução" not in page


def test_health_ignores_unfinished_run(ctx):
    db, _, client = ctx
    run_id = db.start_run("manual", "a")
    db.finish_run(run_id, 0, 0, "b")
    db.start_run("manual", "c")
    assert client.get("/health").json() == {"ok": True, "last_run": "b"}


def test_index_shows_verdict_strip_and_problem_first(ctx):
    db, _, client = ctx
    ok = db.add_repo("owner/aaa")
    gone = db.add_repo("owner/zzz")
    db.save_repo_state(ok.id, RepoState(Status.ACTIVE), checked_at="2026-10-23T06:00:00+00:00",
                       success=True, error=None, size_bytes=1)
    db.save_repo_state(gone.id, RepoState(Status.MISSING, 2), checked_at="t", success=False,
                       error="não encontrado no GitHub (404)", size_bytes=None)
    html = client.get("/").text
    assert "1 repositório sumiu do GitHub." in html
    assert html.index("owner/zzz") < html.index("owner/aaa")
    assert html.count('class="strip"') == 2
    assert "Últimos 30 dias:" in html
    assert "Sumiu do GitHub" in html and "Protegido" in html


def test_index_empty_state(ctx):
    _, _, client = ctx
    html = client.get("/").text
    assert "Nenhum repositório protegido ainda." in html
    assert "Adicione o primeiro repositório acima." in html


def test_architecture_tab(ctx):
    _, _, client = ctx
    assert 'href="/arquitetura"' in client.get("/").text
    page = client.get("/arquitetura")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "GitHub Backup System" in page.text
    assert "<iframe" not in page.text
    assert page.text.count('class="bkp-nav"') == 1
    assert '<a href="/">Repositórios</a>' in page.text
    assert '<a href="/arquitetura" aria-current="page">Arquitetura</a>' in page.text


def test_site_uses_archify_theme_choice(ctx):
    _, _, client = ctx
    html = client.get("/").text
    assert 'localStorage.getItem("archify-theme")' in html
    assert ':root[data-theme="light"]' in html
