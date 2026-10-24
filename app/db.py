import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.state import RepoState, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  id              INTEGER PRIMARY KEY,
  full_name       TEXT NOT NULL UNIQUE,
  status          TEXT NOT NULL DEFAULT 'pending',
  consecutive_404 INTEGER NOT NULL DEFAULT 0,
  consecutive_err INTEGER NOT NULL DEFAULT 0,
  last_success_at TEXT,
  last_checked_at TEXT,
  last_error      TEXT,
  size_bytes      INTEGER,
  enabled         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  trigger     TEXT NOT NULL,
  ok_count    INTEGER NOT NULL DEFAULT 0,
  fail_count  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY,
  repo_id    INTEGER REFERENCES repos(id),
  run_id     INTEGER REFERENCES runs(id),
  kind       TEXT NOT NULL,
  detail     TEXT,
  created_at TEXT NOT NULL,
  emailed    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_pending ON events(emailed, kind);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
"""

_EVENT_SELECT = (
    "SELECT e.*, r.full_name, r.last_success_at, r.last_error "
    "FROM events e LEFT JOIN repos r ON r.id = e.repo_id"
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DuplicateRepoError(Exception):
    pass


@dataclass(frozen=True)
class Repo:
    id: int
    full_name: str
    status: Status
    consecutive_404: int
    consecutive_err: int
    last_success_at: str | None
    last_checked_at: str | None
    last_error: str | None
    size_bytes: int | None
    enabled: bool
    created_at: str

    @property
    def state(self) -> RepoState:
        return RepoState(self.status, self.consecutive_404, self.consecutive_err)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Repo":
        return cls(
            id=row["id"],
            full_name=row["full_name"],
            status=Status(row["status"]),
            consecutive_404=row["consecutive_404"],
            consecutive_err=row["consecutive_err"],
            last_success_at=row["last_success_at"],
            last_checked_at=row["last_checked_at"],
            last_error=row["last_error"],
            size_bytes=row["size_bytes"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Run:
    id: int
    started_at: str
    finished_at: str | None
    trigger: str
    ok_count: int
    fail_count: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        return cls(**{k: row[k] for k in row.keys()})


@dataclass(frozen=True)
class Event:
    id: int
    repo_id: int | None
    run_id: int | None
    kind: str
    detail: str | None
    created_at: str
    emailed: bool
    full_name: str | None
    last_success_at: str | None
    last_error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        data = {k: row[k] for k in row.keys()}
        data["emailed"] = bool(data["emailed"])
        return cls(**data)


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            # WAL lets the web UI read while a backup run writes; busy_timeout makes a
            # briefly locked database wait instead of failing with "database is locked".
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _write(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    # repos

    def add_repo(self, full_name: str, now: str | None = None) -> Repo:
        # The connection context manager commits the read-modify-write as one
        # transaction, or rolls it back if anything in it raises.
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM repos WHERE full_name = ?", (full_name,)
            ).fetchone()
            if row is not None and row["enabled"]:
                raise DuplicateRepoError(full_name)
            if row is not None:
                repo_id = row["id"]
                self._conn.execute("UPDATE repos SET enabled = 1 WHERE id = ?", (repo_id,))
            else:
                cur = self._conn.execute(
                    "INSERT INTO repos (full_name, created_at) VALUES (?, ?)",
                    (full_name, now or utc_now()),
                )
                repo_id = cur.lastrowid
            row = self._conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        return Repo.from_row(row)

    def list_repos(self) -> list[Repo]:
        rows = self._query("SELECT * FROM repos WHERE enabled = 1 ORDER BY full_name")
        return [Repo.from_row(r) for r in rows]

    def get_repo(self, repo_id: int) -> Repo | None:
        rows = self._query("SELECT * FROM repos WHERE id = ?", (repo_id,))
        return Repo.from_row(rows[0]) if rows else None

    def disable_repo(self, repo_id: int) -> bool:
        cur = self._write("UPDATE repos SET enabled = 0 WHERE id = ? AND enabled = 1", (repo_id,))
        return cur.rowcount > 0

    def save_repo_state(
        self,
        repo_id: int,
        state: RepoState,
        *,
        checked_at: str,
        success: bool,
        error: str | None,
        size_bytes: int | None,
    ) -> None:
        self._write(
            """
            UPDATE repos SET
              status = ?, consecutive_404 = ?, consecutive_err = ?, last_checked_at = ?,
              last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
              last_error = ?, size_bytes = COALESCE(?, size_bytes)
            WHERE id = ?
            """,
            (
                state.status.value,
                state.consecutive_404,
                state.consecutive_err,
                checked_at,
                int(success),
                checked_at,
                error,
                size_bytes,
                repo_id,
            ),
        )

    # runs

    def start_run(self, trigger: str, now: str) -> int:
        cur = self._write("INSERT INTO runs (started_at, trigger) VALUES (?, ?)", (now, trigger))
        return cur.lastrowid

    def finish_run(self, run_id: int, ok: int, fail: int, now: str) -> None:
        self._write(
            "UPDATE runs SET finished_at = ?, ok_count = ?, fail_count = ? WHERE id = ?",
            (now, ok, fail, run_id),
        )

    def recent_runs(self, limit: int = 50) -> list[Run]:
        rows = self._query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [Run.from_row(r) for r in rows]

    def last_run(self) -> Run | None:
        runs = self.recent_runs(1)
        return runs[0] if runs else None

    # events

    def add_event(
        self, repo_id: int | None, run_id: int | None, kind: str, detail: str | None, now: str
    ) -> int:
        cur = self._write(
            "INSERT INTO events (repo_id, run_id, kind, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (repo_id, run_id, kind, detail, now),
        )
        return cur.lastrowid

    def pending_email_events(self, kinds: Iterable[str]) -> list[Event]:
        kinds = sorted(kinds)
        marks = ",".join("?" * len(kinds))
        rows = self._query(
            f"{_EVENT_SELECT} WHERE e.emailed = 0 AND e.kind IN ({marks}) ORDER BY e.id", kinds
        )
        return [Event.from_row(r) for r in rows]

    def mark_emailed(self, event_ids: Iterable[int]) -> None:
        ids = list(event_ids)
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self._write(f"UPDATE events SET emailed = 1 WHERE id IN ({marks})", ids)

    def events_for_run(self, run_id: int) -> list[Event]:
        rows = self._query(f"{_EVENT_SELECT} WHERE e.run_id = ? ORDER BY e.id", (run_id,))
        return [Event.from_row(r) for r in rows]

    def events_since(self, since: str) -> list[Event]:
        rows = self._query(f"{_EVENT_SELECT} WHERE e.created_at >= ? ORDER BY e.id", (since,))
        return [Event.from_row(r) for r in rows]

    def last_event_at(self, repo_id: int, kind: str) -> str | None:
        rows = self._query(
            "SELECT MAX(created_at) AS at FROM events WHERE repo_id = ? AND kind = ?",
            (repo_id, kind),
        )
        return rows[0]["at"]
