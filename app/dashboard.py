"""Numbers for the dashboard page: storage per tag/repo, update times and errors."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import Event, Repo, Run
from app.state import Status
from app.ui import _MONTHS, _parse, format_when

WINDOW_DAYS = 30
# Kinds that mean "this repo failed in that run". backup_failing only summarizes
# repeated errors, so counting it would double-count them.
ERROR_KINDS = {"error", "not_found", "missing"}
SUCCESS_KINDS = {"synced", "restored"}


@dataclass(frozen=True)
class Bar:
    label: str
    value: float
    pct: float = 0.0  # share of the largest bar in the chart, 0–100
    note: str = ""


@dataclass(frozen=True)
class Dashboard:
    total_bytes: int
    backed_up_count: int  # repos with a mirror on disk
    repo_count: int
    ok_count: int
    storage_by_tag: list[Bar]
    storage_by_repo: list[Bar]
    run_durations: list[Bar]
    last_run_seconds: float | None
    slowest_repos: list[Bar]
    errors_by_day: list[Bar]
    errors_by_repo: list[Bar]
    error_total: int
    recent_errors: list[Event] = field(default_factory=list)


def _scaled(items: list[tuple[str, float, str]]) -> list[Bar]:
    top = max((value for _, value, _ in items), default=0)
    return [Bar(label, value, value / top * 100 if top else 0.0, note)
            for label, value, note in items]


def _desc(counts: dict[str, float], limit: int | None = None) -> list[tuple[str, float, str]]:
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].casefold()))
    return [(k, v, "") for k, v in ordered[:limit]]


def build_dashboard(
    repos: list[Repo],
    tags: dict[int, list[str]],
    events: list[Event],
    runs: list[Run],
    tz: str,
    now: datetime | None = None,
    top: int = 12,
) -> Dashboard:
    """`events` should cover at least the last WINDOW_DAYS; `runs` newest first."""
    zone = ZoneInfo(tz)
    now = now or datetime.now(UTC)
    today = now.astimezone(zone).date()
    since = now - timedelta(days=WINDOW_DAYS)

    # Storage. A repo with several tags counts in each of them.
    sized = [r for r in repos if r.size_bytes]
    by_tag: dict[str, float] = defaultdict(float)
    for r in sized:
        for tag in tags.get(r.id) or ["Sem tag"]:
            by_tag[tag] += r.size_bytes
    by_repo = sorted(sized, key=lambda r: (-r.size_bytes, r.full_name))
    repo_items = [(r.full_name, r.size_bytes, "") for r in by_repo[:top]]
    rest = by_repo[top:]
    if rest:
        repo_items.append((f"Outros {len(rest)} repositórios",
                           sum(r.size_bytes for r in rest), ""))

    # Update time per run (oldest first) and per repo (latest measurement).
    run_items = []
    for r in reversed(runs):
        if not r.finished_at:
            continue
        seconds = (_parse(r.finished_at) - _parse(r.started_at)).total_seconds()
        if r.aborted:
            note = "interrompida"
        elif r.fail_count:
            note = f"{r.ok_count} ok, {r.fail_count} com problema"
        else:
            note = f"{r.ok_count} ok"
        run_items.append((format_when(r.started_at, tz, now=now), seconds, note))

    # Only successful updates: a check that fails fast says nothing about update time.
    latest_duration: dict[str, tuple[str, float]] = {}
    for e in events:
        if e.kind not in SUCCESS_KINDS or e.duration_ms is None or not e.full_name:
            continue
        current = latest_duration.get(e.full_name)
        if current is None or e.created_at >= current[0]:
            latest_duration[e.full_name] = (e.created_at, e.duration_ms / 1000)

    # Errors in the window.
    errors = [e for e in events if e.kind in ERROR_KINDS and _parse(e.created_at) >= since]
    per_day = Counter(_parse(e.created_at).astimezone(zone).date() for e in errors)
    day_items = []
    for offset in range(WINDOW_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        day_items.append((f"{day.day} {_MONTHS[day.month - 1]}", per_day.get(day, 0), ""))
    per_repo = Counter(e.full_name or "?" for e in errors)

    return Dashboard(
        total_bytes=sum(r.size_bytes for r in sized),
        backed_up_count=len(sized),
        repo_count=len(repos),
        ok_count=sum(1 for r in repos if r.status is Status.ACTIVE),
        storage_by_tag=_scaled(_desc(by_tag)),
        storage_by_repo=_scaled(repo_items),
        run_durations=_scaled(run_items[-WINDOW_DAYS:]),
        last_run_seconds=run_items[-1][1] if run_items else None,
        slowest_repos=_scaled(_desc({k: v for k, (_, v) in latest_duration.items()}, top)),
        errors_by_day=_scaled(day_items),
        errors_by_repo=_scaled(_desc(per_repo, top)),
        error_total=len(errors),
        recent_errors=sorted(errors, key=lambda e: (e.created_at, e.id), reverse=True)[:20],
    )
