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


def test_api_add_repos(ctx):
    db, _, client = ctx
    db.add_repo("owner/existing")
    resp = client.post("/api/repos", json={"repos": [
        "owner/new", "https://github.com/Owner/Other.git", "owner/existing", "not a repo",
        "owner/new",
    ]})
    assert resp.status_code == 201
    assert [(r["input"], r["full_name"], r["status"]) for r in resp.json()["results"]] == [
        ("owner/new", "owner/new", "added"),
        ("https://github.com/Owner/Other.git", "owner/other", "added"),
        ("owner/existing", "owner/existing", "duplicate"),
        ("not a repo", None, "invalid"),
        ("owner/new", "owner/new", "duplicate"),
    ]
    assert [r.full_name for r in db.list_repos()] == ["owner/existing", "owner/new", "owner/other"]


def test_api_add_repos_nothing_added_is_200(ctx):
    db, _, client = ctx
    db.add_repo("owner/repo")
    resp = client.post("/api/repos", json={"repos": ["owner/repo"]})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "duplicate"


@pytest.mark.parametrize(
    "body", [{}, {"repos": []}, {"repos": "owner/repo"}, {"repos": ["a/b"] * 101}, [1, 2]]
)
def test_api_add_repos_rejects_bad_body(ctx, body):
    db, _, client = ctx
    assert client.post("/api/repos", json=body).status_code == 422
    assert db.list_repos() == []


def test_api_add_repos_blocked_cross_site(ctx):
    db, _, client = ctx
    resp = client.post("/api/repos", json={"repos": ["a/b"]},
                       headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    assert db.list_repos() == []


def test_behind_reverse_proxy_domain(tmp_path):
    # Nginx Proxy Manager forwards the original Host; the browser sends a matching https Origin.
    db = Database(tmp_path / "state.db")
    domain = "git-bkp.local.robsonv.com"
    client = TestClient(create_app(db, FakeScheduler(), allowed_hosts=["127.0.0.1", domain]),
                        base_url=f"https://{domain}")
    assert client.get("/").status_code == 200
    resp = client.post("/repos", data={"full_name": "owner/repo"},
                       headers={"Origin": f"https://{domain}", "Sec-Fetch-Site": "same-origin"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?msg=added"
    api = client.post("/api/repos", json={"repos": ["owner/other"]},
                      headers={"Origin": f"https://{domain}"})
    assert api.status_code == 201
    other = TestClient(create_app(db, FakeScheduler(), allowed_hosts=["127.0.0.1", domain]),
                       base_url="https://other.robsonv.com")
    assert other.get("/").status_code == 400
    db.close()


def _tags_by_name(db):
    tags = db.repo_tags()
    return {r.full_name: tags.get(r.id, []) for r in db.list_repos()}


def test_add_repo_form_with_tags(ctx):
    db, _, client = ctx
    resp = client.post("/repos", data={"full_name": "owner/repo", "tags": "Edição, Estudos"},
                       follow_redirects=False)
    assert resp.headers["location"] == "/?msg=added"
    assert _tags_by_name(db) == {"owner/repo": ["Edição", "Estudos"]}


def test_edit_tags_form(ctx):
    db, _, client = ctx
    r = db.add_repo("owner/repo")
    db.add_tags(r.id, ["Old"])
    resp = client.post(f"/repos/{r.id}/edit", data={"tags": "LLM, Skills"}, follow_redirects=False)
    assert resp.headers["location"] == "/?msg=saved"
    assert _tags_by_name(db) == {"owner/repo": ["LLM", "Skills"]}
    bad = client.post(f"/repos/{r.id}/edit", data={"tags": "x" * 41}, follow_redirects=False)
    assert bad.headers["location"] == "/?msg=invalid_edit"
    missing = client.post("/repos/999/edit", data={"tags": "a"}, follow_redirects=False)
    assert missing.headers["location"] == "/?msg=not_found"


def test_index_search_and_tag_filter(ctx):
    db, _, client = ctx
    a = db.add_repo("heygen-com/hyperframes")
    b = db.add_repo("tt-a1i/archify")
    db.add_tags(a.id, ["Edição"])
    db.add_tags(b.id, ["Skills"])
    html = client.get("/").text
    assert "heygen-com/hyperframes" in html and "tt-a1i/archify" in html
    assert 'href="/?tag=Edi%C3%A7%C3%A3o"' in html
    by_tag = client.get("/", params={"tag": "Skills"}).text
    assert "tt-a1i/archify" in by_tag and "heygen-com/hyperframes" not in by_tag
    assert "1 de 2 repositórios" in by_tag
    by_q = client.get("/", params={"q": "hyper"}).text
    assert "heygen-com/hyperframes" in by_q and "tt-a1i/archify" not in by_q
    none = client.get("/", params={"q": "zzz"}).text
    assert "Nenhum repositório corresponde" in none


def test_api_add_repos_with_tags_including_existing(ctx):
    db, _, client = ctx
    db.add_repo("owner/existing")
    resp = client.post("/api/repos", json={"repos": ["owner/new", "owner/existing", "bad"],
                                           "tags": ["Agentes", "Vertex AI"]})
    assert resp.status_code == 201
    assert resp.json()["results"] == [
        {"input": "owner/new", "full_name": "owner/new", "status": "added",
         "description": None, "tags": ["Agentes", "Vertex AI"]},
        {"input": "owner/existing", "full_name": "owner/existing", "status": "duplicate",
         "description": None, "tags": ["Agentes", "Vertex AI"]},
        {"input": "bad", "full_name": None, "status": "invalid", "description": None,
         "tags": []},
    ]
    assert client.post("/api/repos", json={"repos": ["a/b"], "tags": ["x" * 41]}).status_code == 422


def test_api_list_repos(ctx):
    db, _, client = ctx
    a = db.add_repo("heygen-com/hyperframes")
    b = db.add_repo("tt-a1i/archify")
    db.add_tags(a.id, ["Edição"])
    db.add_tags(b.id, ["Skills"])
    body = client.get("/api/repos").json()
    assert [(r["full_name"], r["tags"], r["status"]) for r in body["repos"]] == [
        ("heygen-com/hyperframes", ["Edição"], "pending"),
        ("tt-a1i/archify", ["Skills"], "pending"),
    ]
    assert [r["full_name"] for r in client.get("/api/repos?tag=skills").json()["repos"]] == [
        "tt-a1i/archify"]
    assert [r["full_name"] for r in client.get("/api/repos?q=hyper").json()["repos"]] == [
        "heygen-com/hyperframes"]


def test_api_patch_repo(ctx):
    db, _, client = ctx
    r = db.add_repo("owner/repo")
    db.add_tags(r.id, ["Old"])
    resp = client.patch("/api/repos/Owner/Repo", json={"description": "  Edita vídeos  "})
    assert resp.status_code == 200
    assert resp.json() == {"full_name": "owner/repo", "description": "Edita vídeos",
                           "tags": ["Old"]}
    resp = client.patch("/api/repos/owner/repo", json={"tags": ["New"]})
    assert resp.json() == {"full_name": "owner/repo", "description": "Edita vídeos",
                           "tags": ["New"]}
    resp = client.patch("/api/repos/owner/repo", json={"description": None})
    assert resp.json()["description"] is None
    assert client.patch("/api/repos/owner/nope", json={"tags": []}).status_code == 404
    assert client.patch("/api/repos/owner/repo", json={"tags": ["x" * 41]}).status_code == 422
    assert client.patch("/api/repos/owner/repo",
                        json={"description": "x" * 501}).status_code == 422


def test_api_add_repo_objects_with_description(ctx):
    db, _, client = ctx
    resp = client.post("/api/repos", json={"repos": [
        {"repo": "owner/a", "description": "Primeiro", "tags": ["LLM"]},
        "owner/b",
    ], "tags": ["Estudos"]})
    assert resp.status_code == 201
    got = {r["full_name"]: (r["description"], r["tags"]) for r in resp.json()["results"]}
    assert got == {"owner/a": ("Primeiro", ["Estudos", "LLM"]), "owner/b": (None, ["Estudos"])}
    listed = client.get("/api/repos?q=primeiro").json()["repos"]
    assert [r["full_name"] for r in listed] == ["owner/a"]


def test_edit_form_description_and_tags(ctx):
    db, _, client = ctx
    r = db.add_repo("owner/repo")
    resp = client.post(f"/repos/{r.id}/edit", data={"description": "Clona apps", "tags": "A, B"},
                       follow_redirects=False)
    assert resp.headers["location"] == "/?msg=saved"
    assert db.get_repo(r.id).description == "Clona apps"
    assert _tags_by_name(db) == {"owner/repo": ["A", "B"]}
    html = client.get("/").text
    assert "Clona apps" in html
    client.post(f"/repos/{r.id}/edit", data={"description": "", "tags": ""})
    assert db.get_repo(r.id).description is None


def test_add_form_with_description(ctx):
    db, _, client = ctx
    client.post("/repos", data={"full_name": "owner/repo", "description": "Algo"})
    assert db.list_repos()[0].description == "Algo"
