import json
import logging

from fastapi.testclient import TestClient

from app.main import JsonFormatter, build


def test_build_wires_app(make_settings):
    app, scheduler = build(make_settings())
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/health").json() == {"ok": True, "last_run": None}
    assert {j.id for j in scheduler.jobs()} == {"sync", "summary"}


def test_json_formatter():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    data = json.loads(JsonFormatter().format(record))
    assert data["msg"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "x"


def test_build_closes_unfinished_run_and_skips_catch_up(make_settings):
    from datetime import UTC, datetime, timedelta

    from app.db import Database

    settings = make_settings()
    db = Database(settings.db_path)
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    done = db.start_run("schedule", recent)
    db.finish_run(done, 1, 0, recent)
    db.close()

    _, scheduler = build(settings)
    assert scheduler._needs_catch_up() is False

    db = Database(settings.db_path)
    dangling = db.start_run("manual", recent)
    db.close()
    build(settings)
    db = Database(settings.db_path)
    try:
        run = next(r for r in db.recent_runs() if r.id == dangling)
        assert run.finished_at is not None and run.aborted is True
        assert db.last_finished_run().id == dangling
    finally:
        db.close()


def test_build_rejects_untrusted_host(make_settings):
    client = TestClient(build(make_settings())[0])
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/health", headers={"Host": "127.0.0.1:8080"}).status_code == 200


def test_unwritable_data_dir_keeps_ui_up_and_reports_fix(make_settings, monkeypatch):
    from pathlib import Path

    settings = make_settings()
    real_mkdir = Path.mkdir

    def deny_repos(self, *args, **kwargs):
        if self == settings.repos_dir:
            raise PermissionError(13, "Permission denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_repos)
    app, _ = build(settings)  # must not raise: the UI stays reachable
    client = TestClient(app, base_url="http://127.0.0.1")
    fix = "--entrypoint chown github-bkp-system:latest -R 1000:1000 /data"
    html = client.get("/").text
    assert "Sem permissão para gravar" in html and fix in html
    health = client.get("/health").json()
    assert health["ok"] is True
    assert "Permission denied" in health["problem"] and fix in health["problem"]


def test_writable_data_dir_has_no_problem(make_settings):
    app, _ = build(make_settings())
    client = TestClient(app, base_url="http://127.0.0.1")
    assert "problem" not in client.get("/health").json()
    assert "Sem permissão para gravar" not in client.get("/").text
