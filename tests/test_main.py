import json
import logging

from fastapi.testclient import TestClient

from app.main import JsonFormatter, build


def test_build_wires_app(make_settings):
    app, scheduler = build(make_settings())
    client = TestClient(app)
    assert client.get("/health").json() == {"ok": True, "last_run": None}
    assert {j.id for j in scheduler.jobs()} == {"sync", "summary"}


def test_json_formatter():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    data = json.loads(JsonFormatter().format(record))
    assert data["msg"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "x"
