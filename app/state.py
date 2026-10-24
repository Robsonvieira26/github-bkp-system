from dataclasses import dataclass
from enum import StrEnum

MISSING_THRESHOLD = 2
ERROR_ALERT_THRESHOLD = 3
EMAIL_EVENTS = frozenset({"missing", "restored", "backup_failing"})


class Status(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPECT = "suspect"
    MISSING = "missing"
    ERROR = "error"


class Outcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass(frozen=True)
class RepoState:
    status: Status
    consecutive_404: int = 0
    consecutive_err: int = 0


@dataclass(frozen=True)
class Transition:
    state: RepoState
    events: tuple[str, ...]


def next_state(current: RepoState, outcome: Outcome) -> Transition:
    if outcome is Outcome.OK:
        event = "restored" if current.status is Status.MISSING else "synced"
        return Transition(RepoState(Status.ACTIVE, 0, 0), (event,))

    if outcome is Outcome.NOT_FOUND:
        count = current.consecutive_404 + 1
        if current.status is Status.MISSING:
            return Transition(RepoState(Status.MISSING, count, 0), ())
        if count >= MISSING_THRESHOLD:
            return Transition(RepoState(Status.MISSING, count, 0), ("missing",))
        return Transition(RepoState(Status.SUSPECT, count, 0), ("not_found",))

    errors = current.consecutive_err + 1
    if current.status in (Status.MISSING, Status.SUSPECT):
        return Transition(RepoState(current.status, current.consecutive_404, errors), ("error",))
    events: tuple[str, ...] = ("error",)
    if errors == ERROR_ALERT_THRESHOLD:
        events += ("backup_failing",)
    return Transition(RepoState(Status.ERROR, current.consecutive_404, errors), events)
