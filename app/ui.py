"""Presentation helpers for the web UI: wording, local times and the 30-day history strip."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import Event, Repo
from app.state import Status

HISTORY_DAYS = 30

STATUS_LABELS = {
    Status.ACTIVE: "Protegido",
    Status.PENDING: "Aguardando",
    Status.SUSPECT: "Verificando",
    Status.MISSING: "Sumiu do GitHub",
    Status.ERROR: "Falhando",
}

EVENT_LABELS = {
    "synced": "backup atualizado",
    "refs_preserved": "refs preservadas antes de atualizar",
    "not_found": "não encontrado no GitHub",
    "missing": "sumiu do GitHub",
    "restored": "voltou ao GitHub",
    "error": "falha no backup",
    "backup_failing": "falhando há 3 ciclos",
    "removed": "removido da lista",
}

TRIGGER_LABELS = {"schedule": "Agendada", "manual": "Manual", "startup": "Ao iniciar"}

_MONTHS = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")

# Day cell tone, worst wins: a day with an error and a later success still shows the error.
_EVENT_TONE = {
    "synced": "ok",
    "restored": "ok",
    "refs_preserved": "ok",
    "error": "warn",
    "backup_failing": "warn",
    "not_found": "bad",
    "missing": "bad",
}
_TONE_RANK = {"none": 0, "ok": 1, "warn": 2, "bad": 3}
_TONE_WORDS = {"ok": "backup ok", "warn": "falha", "bad": "não encontrado", "none": "sem registro"}


def _parse(iso: str) -> datetime:
    value = datetime.fromisoformat(iso)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def format_when(value: str | datetime | None, tz: str, now: datetime | None = None) -> str:
    """'hoje, 03:00', 'ontem, 03:00', 'amanhã, 03:00' or '23 out, 03:00' in the given zone."""
    if value is None:
        return "nunca"
    zone = ZoneInfo(tz)
    moment = (_parse(value) if isinstance(value, str) else value).astimezone(zone)
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    clock = moment.strftime("%H:%M")
    delta = (moment.date() - today).days
    if delta == 0:
        return f"hoje, {clock}"
    if delta == -1:
        return f"ontem, {clock}"
    if delta == 1:
        return f"amanhã, {clock}"
    label = f"{moment.day} {_MONTHS[moment.month - 1]}"
    if moment.year != today.year:
        label += f" {moment.year}"
    return f"{label}, {clock}"


_SEVERITY = {
    Status.MISSING: 0, Status.ERROR: 1, Status.SUSPECT: 2, Status.PENDING: 3, Status.ACTIVE: 4,
}


def by_severity(repos: list[Repo]) -> list[Repo]:
    """Repos that need attention first, then alphabetical."""
    return sorted(repos, key=lambda r: (_SEVERITY[r.status], r.full_name))


@dataclass(frozen=True)
class Verdict:
    text: str
    tone: str  # ok | warn | bad | idle


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def verdict(repos: list[Repo]) -> Verdict:
    if not repos:
        return Verdict("Nenhum repositório protegido ainda.", "idle")
    counts = {s: sum(1 for r in repos if r.status is s) for s in Status}
    missing, failing = counts[Status.MISSING], counts[Status.ERROR]
    suspect, pending = counts[Status.SUSPECT], counts[Status.PENDING]
    if missing:
        return Verdict(
            f"{missing} {_plural(missing, 'repositório sumiu', 'repositórios sumiram')} do GitHub.",
            "bad",
        )
    if failing:
        return Verdict(
            f"{failing} {_plural(failing, 'backup está falhando', 'backups estão falhando')}.",
            "warn",
        )
    if suspect:
        subject = _plural(
            suspect, "repositório não foi encontrado", "repositórios não foram encontrados"
        )
        return Verdict(f"{suspect} {subject} no GitHub.", "warn")
    if pending == len(repos):
        return Verdict("Aguardando o primeiro backup.", "idle")
    total = len(repos)
    if total == 1:
        return Verdict("O repositório está protegido.", "ok")
    return Verdict(f"Todos os {total} repositórios estão protegidos.", "ok")


@dataclass(frozen=True)
class DayCell:
    day: date
    tone: str
    title: str


def history_strips(
    repos: list[Repo], events: list[Event], tz: str, now: datetime | None = None,
    days: int = HISTORY_DAYS,
) -> dict[int, list[DayCell]]:
    """Per repo, one cell per local day (oldest first) with the worst outcome recorded that day."""
    zone = ZoneInfo(tz)
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    first = today - timedelta(days=days - 1)
    tones: dict[int, dict[date, str]] = {r.id: {} for r in repos}
    missing_since: dict[int, date] = {}
    for e in events:
        tone = _EVENT_TONE.get(e.kind)
        if tone is None or e.repo_id not in tones:
            continue
        day = _parse(e.created_at).astimezone(zone).date()
        if e.kind == "missing":
            missing_since[e.repo_id] = day
        if day < first:
            continue
        current = tones[e.repo_id].get(day, "none")
        if _TONE_RANK[tone] > _TONE_RANK[current]:
            tones[e.repo_id][day] = tone

    strips = {}
    for repo in repos:
        by_day = tones[repo.id]
        # A missing repo emits no more events, so carry 'bad' forward until today.
        if repo.status is Status.MISSING and repo.id in missing_since:
            day = max(missing_since[repo.id], first)
            while day <= today:
                by_day[day] = "bad"
                day += timedelta(days=1)
        cells = []
        for offset in range(days):
            day = first + timedelta(days=offset)
            tone = by_day.get(day, "none")
            title = f"{day.day} {_MONTHS[day.month - 1]}: {_TONE_WORDS[tone]}"
            cells.append(DayCell(day, tone, title))
        strips[repo.id] = cells
    return strips


def strip_summary(cells: list[DayCell]) -> str:
    ok = sum(1 for c in cells if c.tone == "ok")
    bad = sum(1 for c in cells if c.tone in ("warn", "bad"))
    return f"Últimos {len(cells)} dias: {ok} com backup ok, {bad} com problema."


def filter_repos(repos: list[Repo], tags: dict[int, list[str]], q: str, tag: str) -> list[Repo]:
    """Repos with the tag (exact, any case) whose name, description or tags contain q."""
    needle = q.strip().casefold()
    wanted = tag.strip().casefold()
    result = []
    for repo in repos:
        repo_tags = [t.casefold() for t in tags.get(repo.id, [])]
        if wanted and wanted not in repo_tags:
            continue
        haystack = [repo.full_name, repo.description or "", *repo_tags]
        if needle and not any(needle in text.casefold() for text in haystack):
            continue
        result.append(repo)
    return result
