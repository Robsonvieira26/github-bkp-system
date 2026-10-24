from datetime import UTC, datetime

import pytest

from app.db import Event, Repo
from app.state import Status
from app.ui import (
    by_severity,
    filter_repos,
    format_duration,
    format_when,
    history_strips,
    strip_summary,
    verdict,
)

# 2026-10-23 22:00 in São Paulo (UTC-3)
NOW = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)
TZ = "America/Sao_Paulo"


def repo(id, status=Status.ACTIVE, name=None):
    return Repo(id=id, full_name=name or f"o/r{id}", status=status, consecutive_404=0,
                consecutive_err=0, last_success_at=None, last_checked_at=None, last_error=None,
                size_bytes=None, enabled=True, created_at="x")


def ev(repo_id, kind, at):
    return Event(id=0, repo_id=repo_id, run_id=1, kind=kind, detail=None, created_at=at,
                 emailed=False, full_name=None, last_success_at=None, last_error=None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-23T06:00:00+00:00", "hoje, 03:00"),
        ("2026-10-24T00:30:00+00:00", "hoje, 21:30"),
        ("2026-10-22T06:00:00+00:00", "ontem, 03:00"),
        ("2026-10-24T06:00:00+00:00", "amanhã, 03:00"),
        ("2026-10-02T15:04:00+00:00", "2 out, 12:04"),
        ("2025-12-31T15:00:00+00:00", "31 dez 2025, 12:00"),
        ("2026-10-23T06:00:00", "hoje, 03:00"),
        (None, "nunca"),
    ],
)
def test_format_when(value, expected):
    assert format_when(value, TZ, now=NOW) == expected


def test_format_when_accepts_datetime():
    assert format_when(datetime(2026, 10, 24, 6, tzinfo=UTC), TZ, now=NOW) == "amanhã, 03:00"


@pytest.mark.parametrize(
    ("statuses", "text", "tone"),
    [
        ([], "Nenhum repositório protegido ainda.", "idle"),
        ([Status.ACTIVE], "O repositório está protegido.", "ok"),
        ([Status.ACTIVE, Status.ACTIVE], "Todos os 2 repositórios estão protegidos.", "ok"),
        ([Status.PENDING], "Aguardando o primeiro backup.", "idle"),
        ([Status.ACTIVE, Status.PENDING], "Todos os 2 repositórios estão protegidos.", "ok"),
        ([Status.SUSPECT], "1 repositório não foi encontrado no GitHub.", "warn"),
        ([Status.ERROR, Status.ERROR], "2 backups estão falhando.", "warn"),
        ([Status.ERROR, Status.MISSING], "1 repositório sumiu do GitHub.", "bad"),
        ([Status.MISSING, Status.MISSING], "2 repositórios sumiram do GitHub.", "bad"),
    ],
)
def test_verdict(statuses, text, tone):
    v = verdict([repo(i, s) for i, s in enumerate(statuses)])
    assert (v.text, v.tone) == (text, tone)


def test_by_severity():
    repos = [
        repo(1, Status.ACTIVE, "a/z"),
        repo(2, Status.MISSING, "b/b"),
        repo(3, Status.ACTIVE, "a/a"),
        repo(4, Status.ERROR, "c/c"),
        repo(5, Status.PENDING, "d/d"),
        repo(6, Status.SUSPECT, "e/e"),
    ]
    assert [r.full_name for r in by_severity(repos)] == ["b/b", "c/c", "e/e", "d/d", "a/a", "a/z"]


def test_history_strip_days_and_last_outcome():
    r = repo(1)
    events = [
        ev(1, "synced", "2026-10-23T06:00:00+00:00"),      # today local
        ev(1, "error", "2026-10-22T06:00:00+00:00"),       # yesterday: error then ok
        ev(1, "synced", "2026-10-22T18:00:00+00:00"),
        ev(1, "removed", "2026-10-21T06:00:00+00:00"),     # ignored kind
        ev(1, "synced", "2026-09-01T06:00:00+00:00"),      # outside window
        ev(2, "synced", "2026-10-23T06:00:00+00:00"),      # other repo
    ]
    cells = history_strips([r], events, TZ, now=NOW, days=5)[1]
    assert [c.day.isoformat() for c in cells] == [
        "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
    ]
    assert [c.tone for c in cells] == ["none", "none", "none", "ok", "ok"]
    assert cells[3].title == "22 out: backup ok (depois de 1 falha)"
    assert cells[4].title == "23 out: backup ok"


def test_history_strip_failure_after_success_shows_failure():
    events = [
        ev(1, "synced", "2026-10-23T03:00:00+00:00"),
        ev(1, "error", "2026-10-23T05:00:00+00:00"),
        ev(1, "error", "2026-10-23T06:00:00+00:00"),
        ev(1, "backup_failing", "2026-10-23T06:00:00+00:00"),
        ev(1, "synced", "2026-10-22T03:00:00+00:00"),
        ev(1, "error", "2026-10-22T04:00:00+00:00"),
        ev(1, "error", "2026-10-22T04:30:00+00:00"),
        ev(1, "synced", "2026-10-22T05:00:00+00:00"),
    ]
    cells = history_strips([repo(1)], events, TZ, now=NOW, days=2)[1]
    assert [c.tone for c in cells] == ["ok", "warn"]
    assert cells[0].title == "22 out: backup ok (depois de 2 falhas)"
    assert cells[1].title == "23 out: falha"


def test_history_strip_uses_local_day():
    # 01:00 UTC on the 24th is still the 23rd in São Paulo.
    cells = history_strips([repo(1)], [ev(1, "synced", "2026-10-24T01:00:00+00:00")], TZ,
                           now=NOW, days=2)[1]
    assert [c.tone for c in cells] == ["none", "ok"]


def test_missing_repo_stays_red_until_today():
    r = repo(1, Status.MISSING)
    events = [ev(1, "not_found", "2026-10-20T06:00:00+00:00"),
              ev(1, "missing", "2026-10-21T06:00:00+00:00")]
    cells = history_strips([r], events, TZ, now=NOW, days=5)[1]
    assert [c.tone for c in cells] == ["none", "bad", "bad", "bad", "bad"]


def test_restored_repo_not_carried_forward():
    r = repo(1, Status.ACTIVE)
    events = [ev(1, "missing", "2026-10-20T06:00:00+00:00"),
              ev(1, "restored", "2026-10-22T06:00:00+00:00")]
    cells = history_strips([r], events, TZ, now=NOW, days=5)[1]
    assert [c.tone for c in cells] == ["none", "bad", "none", "ok", "none"]


def test_strip_summary():
    cells = history_strips([repo(1)], [ev(1, "synced", "2026-10-23T06:00:00+00:00"),
                                       ev(1, "error", "2026-10-22T06:00:00+00:00")],
                           TZ, now=NOW, days=4)[1]
    assert strip_summary(cells) == "Últimos 4 dias: 1 com backup ok, 1 com problema."


def test_filter_repos():
    repos = [repo(1, name="heygen-com/hyperframes"), repo(2, name="tt-a1i/archify"),
             repo(3, name="google-research/harness2")]
    tags = {1: ["Edição"], 2: ["Skills"], 3: ["Agentes", "Vertex AI"]}
    names = lambda rs: [r.full_name for r in rs]  # noqa: E731
    assert names(filter_repos(repos, tags, q="", tag="")) == names(repos)
    assert names(filter_repos(repos, tags, q="ARCHIFY", tag="")) == ["tt-a1i/archify"]
    assert names(filter_repos(repos, tags, q="vertex", tag="")) == ["google-research/harness2"]
    assert names(filter_repos(repos, tags, q="", tag="edição")) == ["heygen-com/hyperframes"]
    assert names(filter_repos(repos, tags, q="hyper", tag="Skills")) == []


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, "-"), (0.4, "0,4 s"), (12, "12 s"), (59.6, "60 s"), (150, "2 min 30 s"),
     (600, "10 min"), (3725, "1 h 2 min")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected
