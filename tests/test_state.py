import pytest

from app.state import (
    EMAIL_EVENTS,
    Outcome,
    RepoState,
    Status,
    next_state,
)


def st(status, c404=0, cerr=0):
    return RepoState(Status(status), c404, cerr)


@pytest.mark.parametrize(
    ("current", "outcome", "expected", "events"),
    [
        # OK
        (st("pending"), Outcome.OK, st("active"), ("synced",)),
        (st("active"), Outcome.OK, st("active"), ("synced",)),
        (st("suspect", 1, 1), Outcome.OK, st("active"), ("synced",)),
        (st("error", 0, 5), Outcome.OK, st("active"), ("synced",)),
        (st("missing", 4), Outcome.OK, st("active"), ("restored",)),
        # NOT_FOUND
        (st("pending"), Outcome.NOT_FOUND, st("suspect", 1), ("not_found",)),
        (st("active"), Outcome.NOT_FOUND, st("suspect", 1), ("not_found",)),
        (st("error", 0, 2), Outcome.NOT_FOUND, st("suspect", 1), ("not_found",)),
        (st("suspect", 1, 1), Outcome.NOT_FOUND, st("missing", 2), ("missing",)),
        (st("missing", 2), Outcome.NOT_FOUND, st("missing", 3), ()),
        # ERROR
        (st("active"), Outcome.ERROR, st("error", 0, 1), ("error",)),
        (st("pending"), Outcome.ERROR, st("error", 0, 1), ("error",)),
        (st("error", 0, 1), Outcome.ERROR, st("error", 0, 2), ("error",)),
        (st("error", 0, 2), Outcome.ERROR, st("error", 0, 3), ("error", "backup_failing")),
        (st("error", 0, 3), Outcome.ERROR, st("error", 0, 4), ("error",)),
        (st("suspect", 1), Outcome.ERROR, st("suspect", 1, 1), ("error",)),
        (st("suspect", 1, 2), Outcome.ERROR, st("suspect", 1, 3), ("error",)),
        (st("missing", 2), Outcome.ERROR, st("missing", 2, 1), ("error",)),
    ],
)
def test_transitions(current, outcome, expected, events):
    t = next_state(current, outcome)
    assert t.state == expected
    assert t.events == events


def test_error_between_not_found_does_not_reset_count():
    s = st("active")
    s = next_state(s, Outcome.NOT_FOUND).state
    s = next_state(s, Outcome.ERROR).state
    t = next_state(s, Outcome.NOT_FOUND)
    assert t.state.status is Status.MISSING
    assert t.events == ("missing",)


def test_email_events():
    assert EMAIL_EVENTS == frozenset({"missing", "restored", "backup_failing"})
