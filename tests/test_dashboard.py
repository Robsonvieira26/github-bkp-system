from datetime import UTC, datetime

from app.dashboard import build_dashboard
from app.db import Event, Repo, Run
from app.state import Status

# 2026-10-23 22:00 in São Paulo (UTC-3)
NOW = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)
TZ = "America/Sao_Paulo"
MB = 1024 * 1024


def repo(id, name, size=None, status=Status.ACTIVE):
    return Repo(id=id, full_name=name, status=status, consecutive_404=0, consecutive_err=0,
                last_success_at=None, last_checked_at=None, last_error=None, size_bytes=size,
                enabled=True, created_at="x")


def ev(id, repo_id, name, kind, at, duration_ms=None, detail=None):
    return Event(id=id, repo_id=repo_id, run_id=1, kind=kind, detail=detail, created_at=at,
                 emailed=False, full_name=name, last_success_at=None, last_error=None,
                 duration_ms=duration_ms)


def run(id, started, finished, ok=1, fail=0, aborted=False):
    return Run(id=id, started_at=started, finished_at=finished, trigger="schedule",
               ok_count=ok, fail_count=fail, aborted=aborted)


REPOS = [
    repo(1, "a/video", 300 * MB),
    repo(2, "b/agent", 100 * MB),
    repo(3, "c/both", 50 * MB),
    repo(4, "d/new", None, Status.PENDING),
]
TAGS = {1: ["Edição"], 2: ["Agentes"], 3: ["Agentes", "Edição"]}


def test_storage_totals_and_by_tag():
    d = build_dashboard(REPOS, TAGS, [], [], TZ, now=NOW)
    assert d.total_bytes == 450 * MB
    assert (d.repo_count, d.ok_count) == (4, 3)
    # a repo with two tags counts in both; repos without size are ignored
    assert [(b.label, b.value) for b in d.storage_by_tag] == [
        ("Edição", 350 * MB), ("Agentes", 150 * MB),
    ]
    assert d.storage_by_tag[0].pct == 100.0
    assert round(d.storage_by_tag[1].pct, 1) == 42.9


def test_storage_by_repo_folds_tail_into_other():
    repos = [repo(i, f"o/r{i:02}", (20 - i) * MB) for i in range(1, 16)]
    d = build_dashboard(repos, {}, [], [], TZ, now=NOW, top=12)
    labels = [b.label for b in d.storage_by_repo]
    assert labels[:2] == ["o/r01", "o/r02"]
    assert len(labels) == 13 and labels[-1] == "Outros 3 repositórios"
    assert d.storage_by_repo[-1].value == (7 + 6 + 5) * MB
    assert [b.label for b in d.storage_by_tag] == ["Sem tag"]


def test_run_durations_oldest_first_skipping_unfinished():
    runs = [
        run(3, "2026-10-23T06:00:00+00:00", None),
        run(2, "2026-10-22T06:00:00+00:00", "2026-10-22T06:02:30+00:00", ok=3, fail=1),
        run(1, "2026-10-21T06:00:00+00:00", "2026-10-21T06:00:40+00:00", aborted=True),
    ]
    d = build_dashboard(REPOS, TAGS, [], runs, TZ, now=NOW)
    assert [(b.label, b.value) for b in d.run_durations] == [
        ("21 out, 03:00", 40.0), ("ontem, 03:00", 150.0),
    ]
    assert d.run_durations[1].note == "3 ok, 1 com problema"
    assert d.run_durations[0].note == "interrompida"
    assert d.last_run_seconds == 150.0


def test_slowest_repos_use_latest_duration():
    events = [
        ev(1, 1, "a/video", "synced", "2026-10-21T06:00:00+00:00", 9000),
        ev(2, 1, "a/video", "synced", "2026-10-22T06:00:00+00:00", 4000),
        ev(3, 2, "b/agent", "error", "2026-10-22T06:00:00+00:00", 30000),
        ev(4, 3, "c/both", "synced", "2026-10-22T06:00:00+00:00", 12000),
    ]
    d = build_dashboard(REPOS, TAGS, events, [], TZ, now=NOW)
    # b/agent's failed check is not an update time
    assert [(b.label, b.value) for b in d.slowest_repos] == [("c/both", 12.0), ("a/video", 4.0)]


def test_errors_by_day_repo_and_recent():
    events = [
        ev(1, 2, "b/agent", "error", "2026-10-22T06:00:00+00:00", detail="HTTP 500"),
        ev(2, 2, "b/agent", "backup_failing", "2026-10-22T06:00:00+00:00", detail="HTTP 500"),
        ev(3, 2, "b/agent", "error", "2026-10-23T06:00:00+00:00", detail="HTTP 401"),
        ev(4, 3, "c/both", "not_found", "2026-10-23T07:00:00+00:00", detail="404"),
        ev(5, 1, "a/video", "synced", "2026-10-23T06:00:00+00:00"),
        ev(6, 1, "a/video", "error", "2026-09-01T06:00:00+00:00"),  # outside 30 days
    ]
    d = build_dashboard(REPOS, TAGS, events, [], TZ, now=NOW)
    assert d.error_total == 3  # backup_failing is a summary of errors, not another failure
    by_day = {b.label: b.value for b in d.errors_by_day}
    assert len(d.errors_by_day) == 30
    assert by_day["22 out"] == 1 and by_day["23 out"] == 2 and by_day["21 out"] == 0
    assert [(b.label, b.value) for b in d.errors_by_repo] == [("b/agent", 2), ("c/both", 1)]
    assert [(e.full_name, e.detail) for e in d.recent_errors] == [
        ("c/both", "404"), ("b/agent", "HTTP 401"), ("b/agent", "HTTP 500"),
    ]


def test_empty():
    d = build_dashboard([], {}, [], [], TZ, now=NOW)
    assert d.total_bytes == 0 and d.storage_by_tag == [] and d.storage_by_repo == []
    assert d.run_durations == [] and d.last_run_seconds is None
    assert d.error_total == 0 and all(b.value == 0 for b in d.errors_by_day)
