# GitHub Backup System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Self-hosted Docker service that keeps daily-updated git mirrors of chosen GitHub repos and emails when a repo disappears (or comes back).

**Architecture:** Single Python process in one container: FastAPI Web UI + APScheduler background scheduler + SQLite state, all data under `/data`. A `BackupJob` checks each repo via the GitHub REST API, updates a bare mirror in two phases (staging fetch → preserve vanishing refs under `refs/backup/<date>/` → real fetch), runs a pure state machine, records events and sends grouped emails over SMTP.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, uvicorn, httpx, pydantic-settings, APScheduler 3.x, sqlite3 (stdlib), smtplib (stdlib), git CLI, pytest, ruff, Docker.

**Spec:** `docs/superpowers/specs/2026-10-23-github-backup-design.md`

**Deliberate simplifications vs. spec (YAGNI):**
- Web UI uses plain HTML forms + POST/redirect/GET instead of HTMX (same pages and behavior, no JS dependency).
- Emails are built with `html.escape` + string formatting instead of Jinja2 template files.
- HTTP mocking uses `httpx.MockTransport` instead of `respx`; SMTP is faked by injecting a fake `SMTP` class instead of `aiosmtpd`.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, dependencies, `github-bkp` entry point, pytest/ruff config |
| `app/__init__.py` | Package marker + `__version__` |
| `app/config.py` | `Settings` (pydantic-settings) from env vars |
| `app/names.py` | `parse_repo_name()` — normalize user input to `owner/repo` |
| `app/state.py` | Pure state machine: `Status`, `Outcome`, `RepoState`, `Transition`, `next_state()`, `EMAIL_EVENTS` |
| `app/db.py` | SQLite schema + `Database` access class, dataclasses `Repo`, `Run`, `Event` |
| `app/github_client.py` | `GitHubClient.check_repo()` → `RepoCheck(CheckResult, detail)` |
| `app/mirror.py` | git operations: `mirror_path`, `ensure_mirror`, `sync_mirror`, `repo_size`, `GitError` |
| `app/notifier.py` | `EmailMessage`, `render_events_email`, `render_summary_email`, `Notifier.send()` |
| `app/job.py` | `BackupJob.run()`, `flush_emails()`, `send_summary()` |
| `app/scheduler.py` | `Scheduler` wrapping APScheduler, single-run lock, `trigger_now()` |
| `app/web.py` | `create_app(db, scheduler)` FastAPI routes |
| `app/templates/{base,index,runs}.html` | Jinja2 pages |
| `app/main.py` | `JsonFormatter`, `build(settings)`, `main()` entry point |
| `tests/gitutil.py` | Test helpers: `git()`, `rev()`, `has_ref()`, `RemoteRepo` |
| `tests/conftest.py` | Fixtures: `make_settings`, `remote` |
| `Dockerfile`, `docker-compose.yml`, `.env.example`, `README.md` | Deploy |

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`
- Create: `tests/__init__.py`
- Test: `tests/test_package.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "github-bkp-system"
version = "0.1.0"
description = "Self-hosted daily mirror of GitHub repos with email alerts when a repo disappears"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
    "httpx>=0.27",
    "pydantic-settings>=2.4",
    "apscheduler>=3.10,<4",
]

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.6"]

[project.scripts]
github-bkp = "app.main:main"

[tool.setuptools.packages.find]
include = ["app*"]

[tool.setuptools.package-data]
app = ["templates/*.html"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- [ ] **Step 2: Create a placeholder `README.md`** (setuptools needs it; Task 13 fills it)

```markdown
# github-bkp-system

Self-hosted daily mirror of GitHub repositories with email alerts.
```

- [ ] **Step 3: Write the failing test** — `tests/test_package.py`

```python
import app


def test_version():
    assert app.__version__ == "0.1.0"
```

Also create empty `tests/__init__.py`.

- [ ] **Step 4: Create venv, install, run test to verify it fails**

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/test_package.py -v
```

Expected: FAIL / ERROR — `ModuleNotFoundError: No module named 'app'` (or `AttributeError: __version__`).

- [ ] **Step 5: Implement** — `app/__init__.py`

```python
__version__ = "0.1.0"
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_package.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml README.md app/__init__.py tests/__init__.py tests/test_package.py
git commit -m "chore: project scaffold"
```

---

### Task 2: Settings (`app/config.py`)

**Files:**
- Create: `app/config.py`
- Create: `tests/conftest.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Create `tests/conftest.py`**

```python
import pytest


@pytest.fixture
def make_settings(tmp_path):
    from app.config import Settings

    def factory(**overrides):
        values = dict(
            github_token="test-token",
            smtp_host="smtp.test",
            smtp_user="user",
            smtp_password="pass",
            mail_from="bkp@test.local",
            mail_to="a@test.local, b@test.local",
            data_dir=tmp_path / "data",
        )
        values.update(overrides)
        return Settings(**values)

    return factory
```

- [ ] **Step 2: Write the failing tests** — `tests/test_config.py`

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED_ENV = {
    "GITHUB_TOKEN": "ghp_x",
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USER": "me",
    "SMTP_PASSWORD": "pw",
    "MAIL_FROM": "bkp@example.com",
    "MAIL_TO": "me@example.com",
}


def test_reads_env_with_defaults(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("SMTP_PORT", "SYNC_CRON", "SUMMARY_CRON", "TZ", "DATA_DIR", "SMTP_STARTTLS"):
        monkeypatch.delenv(key, raising=False)

    s = Settings()

    assert s.github_token.get_secret_value() == "ghp_x"
    assert s.smtp_port == 587
    assert s.smtp_starttls is True
    assert s.sync_cron == "0 3 * * *"
    assert s.summary_cron == "0 9 * * 0"
    assert s.tz == "UTC"
    assert s.data_dir == Path("/data")
    assert s.github_api_url == "https://api.github.com"
    assert s.github_git_url == "https://github.com"


def test_missing_required_fails(monkeypatch):
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_mail_to_list_and_paths(make_settings, tmp_path):
    s = make_settings(mail_to=" a@x.com , b@y.com ,")
    assert s.mail_to_list == ["a@x.com", "b@y.com"]
    assert s.db_path == tmp_path / "data" / "state.db"
    assert s.repos_dir == tmp_path / "data" / "repos"


def test_secrets_not_in_repr(make_settings):
    s = make_settings()
    assert "test-token" not in repr(s)
    assert "pass" not in repr(s.smtp_password)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 4: Implement** — `app/config.py`

```python
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    github_token: SecretStr
    smtp_host: str
    smtp_port: int = 587
    smtp_user: str
    smtp_password: SecretStr
    smtp_starttls: bool = True
    mail_from: str
    mail_to: str
    sync_cron: str = "0 3 * * *"
    summary_cron: str = "0 9 * * 0"
    tz: str = "UTC"
    data_dir: Path = Path("/data")
    github_api_url: str = "https://api.github.com"
    github_git_url: str = "https://github.com"

    @property
    def mail_to_list(self) -> list[str]:
        return [addr.strip() for addr in self.mail_to.split(",") if addr.strip()]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add app/config.py tests/conftest.py tests/test_config.py
git commit -m "feat: settings from environment"
```

---

### Task 3: Repo name parsing (`app/names.py`)

**Files:**
- Create: `app/names.py`
- Test: `tests/test_names.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_names.py`

```python
import pytest

from app.names import parse_repo_name


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("owner/repo", "owner/repo"),
        ("  Owner/Repo  ", "owner/repo"),
        ("https://github.com/Owner/My.Repo", "owner/my.repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("http://www.github.com/owner/repo", "owner/repo"),
        ("github.com/owner/repo", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("my-org/some_repo-2", "my-org/some_repo-2"),
    ],
)
def test_valid(text, expected):
    assert parse_repo_name(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "owner",
        "owner/",
        "/repo",
        "owner/repo/extra",
        "-owner/repo",
        "owner/..",
        "owner/.git",
        "https://gitlab.com/owner/repo",
        "owner/re po",
        "a" * 40 + "/repo",
    ],
)
def test_invalid(text):
    with pytest.raises(ValueError):
        parse_repo_name(text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_names.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.names'`

- [ ] **Step 3: Implement** — `app/names.py`

```python
import re

_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_REPO = r"[A-Za-z0-9._-]{1,100}"
_PATTERNS = [
    re.compile(rf"^(?:https?://)?(?:www\.)?github\.com/({_OWNER})/({_REPO})/?$"),
    re.compile(rf"^git@github\.com:({_OWNER})/({_REPO})$"),
    re.compile(rf"^({_OWNER})/({_REPO})$"),
]


def parse_repo_name(text: str) -> str:
    """Normalize user input (owner/repo, HTTPS or SSH URL) to lowercase 'owner/repo'."""
    value = text.strip()
    for pattern in _PATTERNS:
        match = pattern.match(value)
        if not match:
            continue
        owner, repo = match.groups()
        repo = repo.removesuffix(".git")
        if repo in ("", ".", ".."):
            break
        return f"{owner}/{repo}".lower()
    raise ValueError(f"nome de repositório inválido: {text!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_names.py -v`
Expected: 20 passed

- [ ] **Step 5: Commit**

```bash
git add app/names.py tests/test_names.py
git commit -m "feat: parse and normalize repo names"
```

---

### Task 4: State machine (`app/state.py`)

Implements spec §5 transition table. Pure functions, no I/O.

**Files:**
- Create: `app/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_state.py`

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.state'`

- [ ] **Step 3: Implement** — `app/state.py`

```python
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
        return Transition(
            RepoState(current.status, current.consecutive_404, errors), ("error",)
        )
    events: tuple[str, ...] = ("error",)
    if errors == ERROR_ALERT_THRESHOLD:
        events += ("backup_failing",)
    return Transition(RepoState(Status.ERROR, current.consecutive_404, errors), events)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_state.py -v`
Expected: 20 passed

- [ ] **Step 5: Commit**

```bash
git add app/state.py tests/test_state.py
git commit -m "feat: repo status state machine"
```

---

### Task 5: SQLite access (`app/db.py`)

All methods take a lock: the DB is shared by uvicorn worker threads and the scheduler thread.

**Files:**
- Create: `app/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_db.py`

```python
import pytest

from app.db import Database, DuplicateRepoError
from app.state import RepoState, Status

T1 = "2026-10-20T03:00:00+00:00"
T2 = "2026-10-21T03:00:00+00:00"
T3 = "2026-10-22T03:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "sub" / "state.db")
    yield d
    d.close()


def test_add_and_list(db):
    r = db.add_repo("owner/b", now=T1)
    db.add_repo("owner/a", now=T1)
    assert r.status is Status.PENDING
    assert r.enabled is True
    assert r.created_at == T1
    assert [x.full_name for x in db.list_repos()] == ["owner/a", "owner/b"]


def test_duplicate_rejected(db):
    db.add_repo("owner/a")
    with pytest.raises(DuplicateRepoError):
        db.add_repo("owner/a")


def test_disable_and_readd_keeps_history(db):
    r = db.add_repo("owner/a")
    db.save_repo_state(
        r.id, RepoState(Status.ACTIVE), checked_at=T1, success=True, error=None, size_bytes=10
    )
    assert db.disable_repo(r.id) is True
    assert db.list_repos() == []
    again = db.add_repo("owner/a")
    assert again.id == r.id
    assert again.status is Status.ACTIVE
    assert again.last_success_at == T1


def test_save_repo_state(db):
    r = db.add_repo("owner/a")
    db.save_repo_state(
        r.id, RepoState(Status.ACTIVE), checked_at=T1, success=True, error=None, size_bytes=100
    )
    db.save_repo_state(
        r.id,
        RepoState(Status.ERROR, 0, 1),
        checked_at=T2,
        success=False,
        error="boom",
        size_bytes=None,
    )
    got = db.get_repo(r.id)
    assert got.state == RepoState(Status.ERROR, 0, 1)
    assert got.last_success_at == T1
    assert got.last_checked_at == T2
    assert got.last_error == "boom"
    assert got.size_bytes == 100


def test_runs(db):
    assert db.last_run() is None
    run_id = db.start_run("manual", T1)
    db.finish_run(run_id, ok=2, fail=1, now=T2)
    last = db.last_run()
    assert (last.id, last.trigger, last.ok_count, last.fail_count) == (run_id, "manual", 2, 1)
    assert last.finished_at == T2
    db.start_run("schedule", T3)
    assert [r.trigger for r in db.recent_runs(10)] == ["schedule", "manual"]


def test_events_and_email_queue(db):
    r = db.add_repo("owner/a")
    run_id = db.start_run("schedule", T1)
    db.add_event(r.id, run_id, "synced", None, T1)
    e_missing = db.add_event(r.id, run_id, "missing", "404", T2)
    db.add_event(r.id, run_id, "restored", None, T3)

    pending = db.pending_email_events({"missing", "restored"})
    assert [e.kind for e in pending] == ["missing", "restored"]
    assert pending[0].full_name == "owner/a"
    assert pending[0].detail == "404"

    db.mark_emailed([e_missing])
    assert [e.kind for e in db.pending_email_events({"missing", "restored"})] == ["restored"]

    assert [e.kind for e in db.events_for_run(run_id)] == ["synced", "missing", "restored"]
    assert [e.kind for e in db.events_since(T2)] == ["missing", "restored"]
    assert db.last_event_at(r.id, "missing") == T2
    assert db.last_event_at(r.id, "backup_failing") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.db'`

- [ ] **Step 3: Implement** — `app/db.py`

```python
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
        with self._lock:
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
            self._conn.commit()
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_db.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db.py
git commit -m "feat: sqlite state storage"
```

---

### Task 6: GitHub API client (`app/github_client.py`)

**Files:**
- Create: `app/github_client.py`
- Test: `tests/test_github_client.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_github_client.py`

```python
import httpx
import pytest

from app.github_client import CheckResult, GitHubClient


def client_with(handler):
    return GitHubClient("tok", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("status", "result"),
    [
        (200, CheckResult.EXISTS),
        (404, CheckResult.NOT_FOUND),
        (401, CheckResult.ERROR),
        (403, CheckResult.ERROR),
        (500, CheckResult.ERROR),
    ],
)
def test_status_mapping(status, result):
    c = client_with(lambda req: httpx.Response(status, json={}))
    check = c.check_repo("owner/repo")
    assert check.result is result
    if result is CheckResult.ERROR:
        assert check.detail == f"HTTP {status}"


def test_request_shape():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["accept"] = req.headers.get("accept")
        return httpx.Response(200, json={})

    client_with(handler).check_repo("owner/repo")
    assert seen["url"] == "https://api.github.com/repos/owner/repo"
    assert seen["auth"] == "Bearer tok"
    assert seen["accept"] == "application/vnd.github+json"


def test_network_error_is_error():
    def handler(req):
        raise httpx.ConnectTimeout("timeout", request=req)

    check = client_with(handler).check_repo("owner/repo")
    assert check.result is CheckResult.ERROR
    assert check.detail == "rede: ConnectTimeout"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_github_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.github_client'`

- [ ] **Step 3: Implement** — `app/github_client.py`

```python
from dataclasses import dataclass
from enum import StrEnum

import httpx


class CheckResult(StrEnum):
    EXISTS = "exists"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass(frozen=True)
class RepoCheck:
    result: CheckResult
    detail: str = ""


class GitHubClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-bkp-system",
            },
            timeout=timeout,
            transport=transport,
        )

    def check_repo(self, full_name: str) -> RepoCheck:
        try:
            response = self._client.get(f"/repos/{full_name}")
        except httpx.HTTPError as exc:
            return RepoCheck(CheckResult.ERROR, f"rede: {exc.__class__.__name__}")
        if response.status_code == 200:
            return RepoCheck(CheckResult.EXISTS)
        if response.status_code == 404:
            return RepoCheck(CheckResult.NOT_FOUND)
        return RepoCheck(CheckResult.ERROR, f"HTTP {response.status_code}")

    def close(self) -> None:
        self._client.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_github_client.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add app/github_client.py tests/test_github_client.py
git commit -m "feat: github repo existence check"
```

---

### Task 7: Git mirror operations (`app/mirror.py`)

Implements spec §6. Integration tests use local bare repos over `file://` as the "GitHub" origin.

**Files:**
- Create: `app/mirror.py`
- Create: `tests/gitutil.py`
- Modify: `tests/conftest.py` (add `remote` fixture)
- Test: `tests/test_mirror.py`

- [ ] **Step 1: Create test helpers** — `tests/gitutil.py`

```python
import subprocess
from pathlib import Path

_IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.com",
             "-c", "init.defaultBranch=main"]


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *_IDENTITY, *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout


def rev(repo: Path, ref: str) -> str:
    return git("rev-parse", ref, cwd=repo).strip()


def has_ref(repo: Path, ref: str) -> bool:
    proc = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref], cwd=repo, capture_output=True
    )
    return proc.returncode == 0


class RemoteRepo:
    """A bare repo at <base>/<owner>/<repo>.git standing in for GitHub, plus a work clone."""

    def __init__(self, base: Path, full_name: str = "owner/repo"):
        self.base = base
        self.full_name = full_name
        self.bare = base / f"{full_name}.git"
        self.bare.parent.mkdir(parents=True)
        git("init", "--bare", str(self.bare), cwd=base)
        self.work = base / "work"
        git("clone", str(self.bare), str(self.work), cwd=base)
        self.commit("initial")
        self.push("main")

    @property
    def base_url(self) -> str:
        return self.base.as_uri()

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.full_name}.git"

    def commit(self, message: str) -> str:
        (self.work / "file.txt").write_text(message)
        git("add", "-A", cwd=self.work)
        git("commit", "-m", message, cwd=self.work)
        return rev(self.work, "HEAD")

    def push(self, *args: str) -> None:
        git("push", "origin", *args, cwd=self.work)
```

- [ ] **Step 2: Add fixture** — append to `tests/conftest.py`

```python
from tests.gitutil import RemoteRepo


@pytest.fixture
def remote(tmp_path):
    return RemoteRepo(tmp_path / "remote")
```

(Put the `from tests.gitutil import RemoteRepo` line at the top of the file with the other imports.)

- [ ] **Step 3: Write the failing tests** — `tests/test_mirror.py`

```python
from pathlib import Path

import pytest

from app.mirror import GitError, ensure_mirror, mirror_path, repo_size, sync_mirror
from tests.gitutil import git, has_ref, rev

TODAY = "2026-10-23"


@pytest.fixture
def mirror_dir(tmp_path):
    return tmp_path / "mirrors" / "owner" / "repo.git"


def test_mirror_path():
    assert mirror_path(Path("/data/repos"), "owner/repo") == Path("/data/repos/owner/repo.git")


def test_ensure_mirror_clones_with_restricted_refspec(remote, mirror_dir):
    assert ensure_mirror(mirror_dir, remote.url, None) is True
    assert rev(mirror_dir, "refs/heads/main") == rev(remote.work, "HEAD")
    fetch = git("config", "--get-all", "remote.origin.fetch", cwd=mirror_dir).split()
    assert fetch == ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
    assert ensure_mirror(mirror_dir, remote.url, None) is False


def test_ensure_mirror_bad_url_raises(tmp_path, mirror_dir):
    with pytest.raises(GitError):
        ensure_mirror(mirror_dir, (tmp_path / "nope.git").as_uri(), None)


def test_token_not_written_to_disk(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, "super-secret-token")
    sync_mirror(mirror_dir, "super-secret-token", TODAY)
    assert "super-secret-token" not in (mirror_dir / "config").read_text()


def test_sync_fast_forward_preserves_nothing(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    new = remote.commit("second")
    remote.push("main")
    assert sync_mirror(mirror_dir, None, TODAY) == []
    assert rev(mirror_dir, "refs/heads/main") == new
    assert not has_ref(mirror_dir, f"refs/backup/{TODAY}/heads/main")


def test_sync_fetches_new_branch_and_tag(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    git("tag", "v1", cwd=remote.work)
    remote.push("v1")
    git("branch", "dev", cwd=remote.work)
    remote.push("dev")
    sync_mirror(mirror_dir, None, TODAY)
    assert has_ref(mirror_dir, "refs/tags/v1")
    assert has_ref(mirror_dir, "refs/heads/dev")


def test_sync_preserves_deleted_branch(remote, mirror_dir):
    git("checkout", "-b", "feature", cwd=remote.work)
    feat = remote.commit("feature work")
    remote.push("feature")
    git("checkout", "main", cwd=remote.work)
    ensure_mirror(mirror_dir, remote.url, None)

    remote.push("--delete", "feature")
    preserved = sync_mirror(mirror_dir, None, TODAY)

    assert preserved == ["refs/heads/feature"]
    assert not has_ref(mirror_dir, "refs/heads/feature")
    assert rev(mirror_dir, f"refs/backup/{TODAY}/heads/feature") == feat


def test_sync_preserves_force_pushed_branch(remote, mirror_dir):
    lost = remote.commit("will be lost")
    remote.push("main")
    ensure_mirror(mirror_dir, remote.url, None)

    git("reset", "--hard", "HEAD~1", cwd=remote.work)
    rewritten = remote.commit("rewritten")
    remote.push("--force", "main")
    preserved = sync_mirror(mirror_dir, None, TODAY)

    assert preserved == ["refs/heads/main"]
    assert rev(mirror_dir, "refs/heads/main") == rewritten
    assert rev(mirror_dir, f"refs/backup/{TODAY}/heads/main") == lost


def test_sync_preserves_moved_tag(remote, mirror_dir):
    git("tag", "v1", cwd=remote.work)
    remote.push("v1")
    old = rev(remote.work, "v1")
    ensure_mirror(mirror_dir, remote.url, None)

    remote.commit("after tag")
    git("tag", "-f", "v1", cwd=remote.work)
    remote.push("--force", "v1")
    preserved = sync_mirror(mirror_dir, None, TODAY)

    assert preserved == ["refs/tags/v1"]
    assert rev(mirror_dir, f"refs/backup/{TODAY}/tags/v1") == old


def test_backup_refs_survive_later_syncs_and_get_suffix(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    for i in range(2):
        git("branch", "tmp", cwd=remote.work)
        remote.push("tmp")
        sync_mirror(mirror_dir, None, TODAY)
        remote.push("--delete", "tmp")
        git("branch", "-D", "tmp", cwd=remote.work)
        remote.commit(f"move main {i}")
        remote.push("main")
        sync_mirror(mirror_dir, None, TODAY)

    sync_mirror(mirror_dir, None, TODAY)
    assert has_ref(mirror_dir, f"refs/backup/{TODAY}/heads/tmp")
    assert has_ref(mirror_dir, f"refs/backup/{TODAY}/heads/tmp-2")


def test_incoming_refs_cleaned(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    sync_mirror(mirror_dir, None, TODAY)
    refs = git("for-each-ref", "--format=%(refname)", cwd=mirror_dir).split()
    assert not [r for r in refs if r.startswith("refs/incoming/")]


def test_repo_size(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    assert repo_size(mirror_dir) > 0
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/test_mirror.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.mirror'`

- [ ] **Step 5: Implement** — `app/mirror.py`

```python
import base64
import os
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 30 * 60
INCOMING = "refs/incoming/"
FETCH_REFSPECS = ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
STAGING_REFSPECS = ("+refs/heads/*:refs/incoming/heads/*", "+refs/tags/*:refs/incoming/tags/*")


class GitError(Exception):
    pass


def mirror_path(repos_dir: Path, full_name: str) -> Path:
    owner, repo = full_name.split("/", 1)
    return repos_dir / owner / f"{repo}.git"


def _run(
    args: list[str],
    cwd: Path | None = None,
    token: str | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        # Passed via env so the token never lands in .git/config or the process list.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        input=input,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )


def _git(args: list[str], cwd: Path | None = None, token: str | None = None,
         input: str | None = None) -> str:
    proc = _run(args, cwd=cwd, token=token, input=input)
    if proc.returncode != 0:
        raise GitError(f"git {args[0]} falhou: {proc.stderr.strip()[:500]}")
    return proc.stdout


def _list_refs(path: Path, *prefixes: str) -> dict[str, str]:
    out = _git(["for-each-ref", "--format=%(objectname) %(refname)", *prefixes], cwd=path)
    refs = {}
    for line in out.splitlines():
        sha, name = line.split(" ", 1)
        refs[name] = sha
    return refs


def _delete_refs(path: Path, prefix: str) -> None:
    refs = _list_refs(path, prefix)
    if refs:
        _git(["update-ref", "--stdin"], cwd=path,
             input="".join(f"delete {name}\n" for name in refs))


def _is_ancestor(path: Path, old: str, new: str) -> bool:
    proc = _run(["merge-base", "--is-ancestor", old, new], cwd=path)
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise GitError(f"git merge-base falhou: {proc.stderr.strip()[:500]}")


def _unique_ref(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def ensure_mirror(path: Path, url: str, token: str | None) -> bool:
    """Clone a bare mirror if missing. Returns True when a new clone was created."""
    if (path / "HEAD").exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--mirror", url, str(path)], token=token)
    # The default mirror refspec (+refs/*:refs/*) would let --prune delete refs/backup/*.
    _git(["config", "--unset-all", "remote.origin.fetch"], cwd=path)
    for spec in FETCH_REFSPECS:
        _git(["config", "--add", "remote.origin.fetch", spec], cwd=path)
    _git(["config", "remote.origin.mirror", "false"], cwd=path)
    _delete_refs(path, "refs/pull/")
    return True


def _preserve_vanishing_refs(path: Path, today: str) -> list[str]:
    local = _list_refs(path, "refs/heads/", "refs/tags/")
    incoming = _list_refs(path, INCOMING)
    existing_backups = set(_list_refs(path, "refs/backup/"))
    preserved = []
    for name, sha in sorted(local.items()):
        short = name.removeprefix("refs/")
        new = incoming.get(INCOMING + short)
        if new == sha:
            continue
        if new is not None and name.startswith("refs/heads/") and _is_ancestor(path, sha, new):
            continue
        target = _unique_ref(f"refs/backup/{today}/{short}", existing_backups)
        _git(["update-ref", target, sha], cwd=path)
        existing_backups.add(target)
        preserved.append(name)
    return preserved


def sync_mirror(path: Path, token: str | None, today: str) -> list[str]:
    """Update the mirror, first copying refs that would vanish to refs/backup/<today>/.

    Returns the original names of the preserved refs.
    """
    # --no-tags: tag auto-following would write refs/tags/* before we compare.
    _git(["fetch", "--prune", "--no-tags", "origin", *STAGING_REFSPECS], cwd=path, token=token)
    try:
        preserved = _preserve_vanishing_refs(path, today)
        _git(["fetch", "--prune", "origin"], cwd=path, token=token)
    finally:
        _delete_refs(path, INCOMING)
    return preserved


def repo_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_mirror.py -v`
Expected: 12 passed

- [ ] **Step 7: Commit**

```bash
git add app/mirror.py tests/gitutil.py tests/conftest.py tests/test_mirror.py
git commit -m "feat: git mirror with vanishing-ref preservation"
```

---

### Task 8: Email rendering and sending (`app/notifier.py`)

**Files:**
- Create: `app/notifier.py`
- Test: `tests/test_notifier.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_notifier.py`

```python
from pathlib import Path

from app.db import Event, Repo
from app.notifier import EmailMessage, Notifier, render_events_email, render_summary_email
from app.state import Status


def ev(kind, name="owner/repo", detail=None, last_success="2026-10-20T03:00:00+00:00",
       last_error=None, id=1):
    return Event(id=id, repo_id=1, run_id=1, kind=kind, detail=detail,
                 created_at="2026-10-23T03:00:00+00:00", emailed=False, full_name=name,
                 last_success_at=last_success, last_error=last_error)


def repo(name, status=Status.ACTIVE, size=2048):
    return Repo(id=1, full_name=name, status=status, consecutive_404=0, consecutive_err=0,
                last_success_at="2026-10-23T03:00:00+00:00", last_checked_at=None,
                last_error=None, size_bytes=size, enabled=True, created_at="x")


def make_fake_smtp():
    created = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            self.host, self.port, self.calls, self.sent = host, port, [], []
            created.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            self.calls.append("starttls")

        def login(self, user, password):
            self.calls.append(("login", user, password))

        def send_message(self, msg):
            self.sent.append(msg)

    return FakeSMTP, created


def test_missing_email():
    msg = render_events_email([ev("missing")], Path("/data/repos"))
    assert msg.subject == "[github-bkp] 1 repositório(s) sumiu(ram) do GitHub"
    assert "owner/repo não existe mais no GitHub" in msg.text
    assert "2026-10-20T03:00:00+00:00" in msg.text
    assert str(Path("/data/repos/owner/repo.git")) in msg.text
    assert "git clone" in msg.text
    assert "owner/repo" in msg.html


def test_mixed_events_grouped_in_one_email():
    events = [
        ev("missing", "a/one", id=1),
        ev("restored", "b/two", detail="marcado como sumido em 2026-10-01", id=2),
        ev("backup_failing", "c/three", last_error="HTTP 500", id=3),
    ]
    msg = render_events_email(events, Path("/data/repos"))
    assert msg.subject == "[github-bkp] 3 alerta(s) de backup"
    assert "a/one" in msg.text and "b/two voltou" in msg.text
    assert "marcado como sumido em 2026-10-01" in msg.text
    assert "c/three" in msg.text and "HTTP 500" in msg.text


def test_html_is_escaped():
    msg = render_events_email([ev("backup_failing", last_error="<script>")], Path("/r"))
    assert "<script>" not in msg.html
    assert "&lt;script&gt;" in msg.html


def test_summary_email():
    msg = render_summary_email(
        [repo("owner/a"), repo("owner/b", Status.MISSING, None)],
        [ev("synced"), ev("synced"), ev("missing")],
    )
    assert msg.subject == "[github-bkp] Resumo semanal: 2 repositório(s)"
    assert "owner/a" in msg.text and "2.0 KB" in msg.text
    assert "missing" in msg.text
    assert "synced: 2" in msg.text and "missing: 1" in msg.text


def test_send_starttls(make_settings):
    smtp, created = make_fake_smtp()
    n = Notifier(make_settings(smtp_port=587), smtp_factory=smtp, smtp_ssl_factory=smtp)
    n.send(EmailMessage("Hi", "text", "<p>html</p>"))
    conn = created[0]
    assert (conn.host, conn.port) == ("smtp.test", 587)
    assert conn.calls == ["starttls", ("login", "user", "pass")]
    sent = conn.sent[0]
    assert sent["Subject"] == "Hi"
    assert sent["From"] == "bkp@test.local"
    assert sent["To"] == "a@test.local, b@test.local"


def test_send_ssl(make_settings):
    plain, plain_created = make_fake_smtp()
    ssl_cls, ssl_created = make_fake_smtp()
    n = Notifier(make_settings(smtp_port=465, smtp_starttls=False),
                 smtp_factory=plain, smtp_ssl_factory=ssl_cls)
    n.send(EmailMessage("Hi", "text", "<p>html</p>"))
    assert plain_created == []
    assert ssl_created[0].calls == [("login", "user", "pass")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_notifier.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.notifier'`

- [ ] **Step 3: Implement** — `app/notifier.py`

```python
import smtplib
import ssl
from collections import Counter
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from html import escape
from pathlib import Path

from app.config import Settings
from app.db import Event, Repo
from app.mirror import mirror_path

PREFIX = "[github-bkp]"


@dataclass(frozen=True)
class EmailMessage:
    subject: str
    text: str
    html: str


def _html(blocks: list[str]) -> str:
    body = "".join(f"<pre>{escape(b)}</pre>" for b in blocks)
    return f"<html><body>{body}</body></html>"


def _describe(event: Event, repos_dir: Path) -> str:
    name = event.full_name or "?"
    last_ok = event.last_success_at or "nunca"
    if event.kind == "missing":
        path = mirror_path(repos_dir, name)
        repo = name.split("/", 1)[-1]
        return (
            f"{name} não existe mais no GitHub (404 em 2 verificações seguidas).\n"
            f"  Último backup ok: {last_ok}\n"
            f"  Cópia local: {path}\n"
            f"  Restaurar: git clone {path} {repo}"
        )
    if event.kind == "restored":
        suffix = f" ({event.detail})" if event.detail else ""
        return f"{name} voltou a existir no GitHub{suffix}."
    if event.kind == "backup_failing":
        return (
            f"{name}: backup falhando há 3 ciclos seguidos.\n"
            f"  Último erro: {event.last_error or event.detail or '-'}\n"
            f"  Último backup ok: {last_ok}"
        )
    return f"{name}: {event.kind} {event.detail or ''}".strip()


def render_events_email(events: list[Event], repos_dir: Path) -> EmailMessage:
    count = len(events)
    if {e.kind for e in events} == {"missing"}:
        subject = f"{PREFIX} {count} repositório(s) sumiu(ram) do GitHub"
    else:
        subject = f"{PREFIX} {count} alerta(s) de backup"
    blocks = [_describe(e, repos_dir) for e in events]
    return EmailMessage(subject, "\n\n".join(blocks), _html(blocks))


def format_size(size: int | None) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def render_summary_email(repos: list[Repo], events: list[Event]) -> EmailMessage:
    lines = [f"{'Repositório':<45} {'Status':<8} {'Último sync ok':<26} Tamanho"]
    for r in repos:
        lines.append(
            f"{r.full_name:<45} {r.status.value:<8} {r.last_success_at or '-':<26} "
            f"{format_size(r.size_bytes)}"
        )
    counts = Counter(e.kind for e in events)
    event_lines = [f"{kind}: {n}" for kind, n in sorted(counts.items())] or ["nenhum evento"]
    blocks = ["\n".join(lines), "Eventos nos últimos 7 dias:\n" + "\n".join(event_lines)]
    subject = f"{PREFIX} Resumo semanal: {len(repos)} repositório(s)"
    return EmailMessage(subject, "\n\n".join(blocks), _html(blocks))


class Notifier:
    def __init__(self, settings: Settings, smtp_factory=smtplib.SMTP,
                 smtp_ssl_factory=smtplib.SMTP_SSL):
        self._settings = settings
        self._smtp = smtp_factory
        self._smtp_ssl = smtp_ssl_factory

    def send(self, message: EmailMessage) -> None:
        s = self._settings
        mime = MimeMessage()
        mime["Subject"] = message.subject
        mime["From"] = s.mail_from
        mime["To"] = ", ".join(s.mail_to_list)
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")
        context = ssl.create_default_context()
        password = s.smtp_password.get_secret_value()
        if s.smtp_starttls:
            with self._smtp(s.smtp_host, s.smtp_port, timeout=30) as conn:
                conn.starttls(context=context)
                conn.login(s.smtp_user, password)
                conn.send_message(mime)
        else:
            with self._smtp_ssl(s.smtp_host, s.smtp_port, timeout=30, context=context) as conn:
                conn.login(s.smtp_user, password)
                conn.send_message(mime)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_notifier.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/notifier.py tests/test_notifier.py
git commit -m "feat: email rendering and SMTP sending"
```

---

### Task 9: Backup job orchestration (`app/job.py`)

**Files:**
- Create: `app/job.py`
- Test: `tests/test_job.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_job.py`

```python
import smtplib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.db import Database
from app.github_client import CheckResult, RepoCheck
from app.job import BackupJob
from app.mirror import mirror_path
from app.state import Status
from tests.gitutil import git, has_ref


class FakeGitHub:
    def __init__(self):
        self.results: dict[str, RepoCheck] = {}

    def check_repo(self, full_name):
        return self.results.get(full_name, RepoCheck(CheckResult.EXISTS))


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, message):
        if self.fail:
            raise smtplib.SMTPException("down")
        self.sent.append(message)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def next_day(self):
        self.now += timedelta(days=1)


@pytest.fixture
def env(tmp_path, remote):
    db = Database(tmp_path / "state.db")
    gh, nt, clock = FakeGitHub(), FakeNotifier(), Clock()
    repos_dir = tmp_path / "repos"
    job = BackupJob(db, gh, nt, repos_dir=repos_dir, git_base_url=remote.base_url,
                    token=None, now=clock)
    yield SimpleNamespace(db=db, gh=gh, nt=nt, clock=clock, job=job, remote=remote,
                          repos_dir=repos_dir)
    db.close()


def run_days(env, n):
    for _ in range(n):
        env.job.run("schedule")
        env.clock.next_day()


def kinds(env, run_id):
    return [e.kind for e in env.db.events_for_run(run_id)]


def test_new_repo_is_mirrored(env):
    r = env.db.add_repo("owner/repo")
    run_id = env.job.run("manual")
    got = env.db.get_repo(r.id)
    assert got.status is Status.ACTIVE
    assert got.last_success_at == "2026-10-23T03:00:00+00:00"
    assert got.size_bytes > 0
    assert has_ref(mirror_path(env.repos_dir, "owner/repo"), "refs/heads/main")
    assert kinds(env, run_id) == ["synced"]
    run = env.db.last_run()
    assert (run.trigger, run.ok_count, run.fail_count) == ("manual", 1, 0)
    assert env.nt.sent == []


def test_missing_after_two_not_found(env):
    r = env.db.add_repo("owner/repo")
    run_days(env, 1)
    env.gh.results["owner/repo"] = RepoCheck(CheckResult.NOT_FOUND)

    env.job.run("schedule")
    assert env.db.get_repo(r.id).status is Status.SUSPECT
    assert env.nt.sent == []

    env.clock.next_day()
    env.job.run("schedule")
    assert env.db.get_repo(r.id).status is Status.MISSING
    assert len(env.nt.sent) == 1
    assert "sumiu" in env.nt.sent[0].subject
    assert "owner/repo" in env.nt.sent[0].text

    env.clock.next_day()
    env.job.run("schedule")
    assert len(env.nt.sent) == 1  # no repeat


def test_restored_sends_email(env):
    r = env.db.add_repo("owner/repo")
    env.gh.results["owner/repo"] = RepoCheck(CheckResult.NOT_FOUND)
    run_days(env, 2)
    del env.gh.results["owner/repo"]
    env.job.run("schedule")
    assert env.db.get_repo(r.id).status is Status.ACTIVE
    assert len(env.nt.sent) == 2
    assert "owner/repo voltou" in env.nt.sent[1].text
    assert "marcado como sumido em 2026-10-24" in env.nt.sent[1].text


def test_backup_failing_after_three_errors(env):
    r = env.db.add_repo("owner/repo")
    env.gh.results["owner/repo"] = RepoCheck(CheckResult.ERROR, "HTTP 500")
    run_days(env, 2)
    assert env.nt.sent == []
    run_days(env, 1)
    assert len(env.nt.sent) == 1
    assert "HTTP 500" in env.nt.sent[0].text
    run_days(env, 1)
    assert len(env.nt.sent) == 1
    assert env.db.get_repo(r.id).last_error == "HTTP 500"


def test_git_failure_is_error(env):
    r = env.db.add_repo("owner/does-not-exist-locally")
    env.job.run("schedule")
    got = env.db.get_repo(r.id)
    assert got.status is Status.ERROR
    assert got.last_error.startswith("git clone falhou")
    assert env.db.last_run().fail_count == 1


def test_events_from_one_run_grouped(env):
    env.db.add_repo("owner/a")
    env.db.add_repo("owner/b")
    env.gh.results["owner/a"] = RepoCheck(CheckResult.NOT_FOUND)
    env.gh.results["owner/b"] = RepoCheck(CheckResult.NOT_FOUND)
    run_days(env, 2)
    assert len(env.nt.sent) == 1
    assert "owner/a" in env.nt.sent[0].text and "owner/b" in env.nt.sent[0].text


def test_failed_email_is_retried(env):
    env.db.add_repo("owner/repo")
    env.gh.results["owner/repo"] = RepoCheck(CheckResult.NOT_FOUND)
    env.nt.fail = True
    run_days(env, 2)
    assert env.nt.sent == []
    env.nt.fail = False
    env.job.run("schedule")
    assert len(env.nt.sent) == 1
    assert "owner/repo" in env.nt.sent[0].text


def test_refs_preserved_event(env):
    r = env.db.add_repo("owner/repo")
    git("branch", "gone", cwd=env.remote.work)
    env.remote.push("gone")
    run_days(env, 1)
    env.remote.push("--delete", "gone")
    run_id = env.job.run("schedule")
    events = env.db.events_for_run(run_id)
    assert [e.kind for e in events] == ["refs_preserved", "synced"]
    assert events[0].detail == "refs/heads/gone"
    assert env.db.get_repo(r.id).status is Status.ACTIVE


def test_unexpected_exception_does_not_stop_run(env, monkeypatch):
    env.db.add_repo("owner/a")
    env.db.add_repo("owner/repo")

    real = env.gh.check_repo

    def flaky(name):
        if name == "owner/a":
            raise RuntimeError("bug")
        return real(name)

    monkeypatch.setattr(env.gh, "check_repo", flaky)
    env.job.run("schedule")
    statuses = {r.full_name: r.status for r in env.db.list_repos()}
    assert statuses == {"owner/a": Status.ERROR, "owner/repo": Status.ACTIVE}


def test_send_summary(env):
    env.db.add_repo("owner/repo")
    env.job.run("schedule")
    env.job.send_summary()
    assert len(env.nt.sent) == 1
    assert env.nt.sent[0].subject.startswith("[github-bkp] Resumo semanal")
    assert "synced: 1" in env.nt.sent[0].text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_job.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.job'`

- [ ] **Step 3: Implement** — `app/job.py`

```python
import logging
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.db import Database, Repo
from app.github_client import CheckResult, RepoCheck
from app.mirror import GitError, ensure_mirror, mirror_path, repo_size, sync_mirror
from app.notifier import EmailMessage, render_events_email, render_summary_email
from app.state import EMAIL_EVENTS, Outcome, next_state

log = logging.getLogger(__name__)

NOT_FOUND_MSG = "não encontrado no GitHub (404)"


class RepoChecker(Protocol):
    def check_repo(self, full_name: str) -> RepoCheck: ...


class Sender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class BackupJob:
    def __init__(
        self,
        db: Database,
        github: RepoChecker,
        notifier: Sender,
        *,
        repos_dir: Path,
        git_base_url: str,
        token: str | None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._db = db
        self._github = github
        self._notifier = notifier
        self._repos_dir = repos_dir
        self._git_base_url = git_base_url.rstrip("/")
        self._token = token
        self._now = now

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def run(self, trigger: str) -> int:
        run_id = self._db.start_run(trigger, self._stamp())
        ok = fail = 0
        for repo in self._db.list_repos():
            if self._process(repo, run_id):
                ok += 1
            else:
                fail += 1
        self._db.finish_run(run_id, ok, fail, self._stamp())
        log.info("run %s finished: ok=%s fail=%s", run_id, ok, fail)
        self.flush_emails()
        return run_id

    def _process(self, repo: Repo, run_id: int) -> bool:
        stamp = self._stamp()
        size = None
        try:
            outcome, error, size = self._sync(repo, run_id, stamp)
        except Exception as exc:
            log.exception("unexpected error processing %s", repo.full_name)
            outcome, error = Outcome.ERROR, f"erro inesperado: {exc.__class__.__name__}"

        transition = next_state(repo.state, outcome)
        for kind in transition.events:
            detail = error if kind in ("error", "backup_failing", "not_found", "missing") else None
            if kind == "restored":
                since = self._db.last_event_at(repo.id, "missing")
                detail = f"marcado como sumido em {since[:10]}" if since else None
            self._db.add_event(repo.id, run_id, kind, detail, stamp)
        self._db.save_repo_state(
            repo.id,
            transition.state,
            checked_at=stamp,
            success=outcome is Outcome.OK,
            error=error,
            size_bytes=size,
        )
        log.info("%s: %s -> %s", repo.full_name, outcome.value, transition.state.status.value)
        return outcome is Outcome.OK

    def _sync(self, repo: Repo, run_id: int, stamp: str) -> tuple[Outcome, str | None, int | None]:
        check = self._github.check_repo(repo.full_name)
        if check.result is CheckResult.NOT_FOUND:
            return Outcome.NOT_FOUND, NOT_FOUND_MSG, None
        if check.result is CheckResult.ERROR:
            return Outcome.ERROR, check.detail, None

        path = mirror_path(self._repos_dir, repo.full_name)
        url = f"{self._git_base_url}/{repo.full_name}.git"
        try:
            ensure_mirror(path, url, self._token)
            preserved = sync_mirror(path, self._token, stamp[:10])
        except GitError as exc:
            return Outcome.ERROR, str(exc), None
        except subprocess.TimeoutExpired:
            return Outcome.ERROR, "git timeout", None
        if preserved:
            self._db.add_event(repo.id, run_id, "refs_preserved", ", ".join(preserved), stamp)
        return Outcome.OK, None, repo_size(path)

    def flush_emails(self) -> None:
        events = self._db.pending_email_events(EMAIL_EVENTS)
        if not events:
            return
        try:
            self._notifier.send(render_events_email(events, self._repos_dir))
        except Exception:
            log.exception("failed to send alert email; will retry next run")
            return
        self._db.mark_emailed(e.id for e in events)

    def send_summary(self) -> None:
        since = (self._now() - timedelta(days=7)).isoformat(timespec="seconds")
        try:
            message = render_summary_email(self._db.list_repos(), self._db.events_since(since))
            self._notifier.send(message)
        except Exception:
            log.exception("failed to send weekly summary")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_job.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add app/job.py tests/test_job.py
git commit -m "feat: backup job orchestration"
```

---

### Task 10: Scheduler (`app/scheduler.py`)

**Files:**
- Create: `app/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_scheduler.py`

```python
import threading

import pytest

from app.scheduler import Scheduler


class BlockingJob:
    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.triggers = []
        self.summaries = 0

    def run(self, trigger):
        self.triggers.append(trigger)
        self.started.set()
        self.release.wait(5)
        self.finished.set()

    def send_summary(self):
        self.summaries += 1


def make(job):
    return Scheduler(job, sync_cron="0 3 * * *", summary_cron="0 9 * * 0", tz="UTC")


def test_trigger_now_runs_once_at_a_time():
    job = BlockingJob()
    s = make(job)
    assert s.trigger_now() is True
    assert job.started.wait(5)
    assert s.is_running() is True
    assert s.trigger_now() is False
    job.release.set()
    assert job.finished.wait(5)
    for _ in range(50):
        if not s.is_running():
            break
        threading.Event().wait(0.05)
    assert s.is_running() is False
    assert job.triggers == ["manual"]


def test_scheduled_run_skips_when_busy():
    job = BlockingJob()
    s = make(job)
    s.trigger_now()
    assert job.started.wait(5)
    s._scheduled_sync()  # returns immediately, does not block or run
    assert job.triggers == ["manual"]
    job.release.set()


def test_scheduled_run_uses_schedule_trigger():
    job = BlockingJob()
    job.release.set()
    s = make(job)
    s._scheduled_sync()
    assert job.triggers == ["schedule"]
    assert s.is_running() is False


def test_jobs_registered():
    s = make(BlockingJob())
    assert {j.id for j in s.jobs()} == {"sync", "summary"}


def test_invalid_cron_rejected():
    with pytest.raises(ValueError):
        Scheduler(BlockingJob(), sync_cron="not a cron", summary_cron="0 9 * * 0", tz="UTC")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.scheduler'`

- [ ] **Step 3: Implement** — `app/scheduler.py`

```python
import logging
import threading
from typing import Protocol

from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)


class RunnableJob(Protocol):
    def run(self, trigger: str) -> int: ...

    def send_summary(self) -> None: ...


class Scheduler:
    def __init__(self, job: RunnableJob, *, sync_cron: str, summary_cron: str, tz: str):
        self._job = job
        self._lock = threading.Lock()
        self._sched = BackgroundScheduler(timezone=tz)
        self._sched.add_job(
            self._scheduled_sync,
            CronTrigger.from_crontab(sync_cron, timezone=tz),
            id="sync",
            max_instances=1,
            coalesce=True,
        )
        self._sched.add_job(
            job.send_summary,
            CronTrigger.from_crontab(summary_cron, timezone=tz),
            id="summary",
            max_instances=1,
            coalesce=True,
        )

    def start(self) -> None:
        self._sched.start()

    def shutdown(self) -> None:
        self._sched.shutdown(wait=False)

    def jobs(self) -> list[Job]:
        return self._sched.get_jobs()

    def is_running(self) -> bool:
        return self._lock.locked()

    def trigger_now(self) -> bool:
        """Start a manual run in the background. False if a run is already in progress."""
        if not self._lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._run_locked, args=("manual",), daemon=True).start()
        return True

    def _scheduled_sync(self) -> None:
        if not self._lock.acquire(blocking=False):
            log.warning("scheduled sync skipped: a run is already in progress")
            return
        self._run_locked("schedule")

    def _run_locked(self, trigger: str) -> None:
        try:
            self._job.run(trigger)
        except Exception:
            log.exception("backup run failed")
        finally:
            self._lock.release()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add app/scheduler.py tests/test_scheduler.py
git commit -m "feat: daily scheduler with single-run lock"
```

---

### Task 11: Web UI (`app/web.py` + templates)

**Files:**
- Create: `app/web.py`
- Create: `app/templates/base.html`
- Create: `app/templates/index.html`
- Create: `app/templates/runs.html`
- Test: `tests/test_web.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_web.py`

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.web'`

- [ ] **Step 3: Implement** — `app/web.py`

```python
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import Database, DuplicateRepoError
from app.names import parse_repo_name
from app.notifier import format_size

TEMPLATES_DIR = Path(__file__).parent / "templates"

MESSAGES = {
    "added": "Repositório adicionado.",
    "removed": "Repositório removido (cópia local mantida).",
    "sync_started": "Sincronização iniciada.",
    "sync_busy": "Sincronização já em execução.",
    "invalid": "Nome inválido. Use owner/repo ou a URL do GitHub.",
    "duplicate": "Repositório já está na lista.",
}


class SchedulerLike(Protocol):
    def is_running(self) -> bool: ...

    def trigger_now(self) -> bool: ...


def _redirect(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/?msg={msg}", status_code=303)


def create_app(db: Database, scheduler: SchedulerLike) -> FastAPI:
    app = FastAPI(title="github-bkp", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.filters["size"] = format_size

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, msg: str | None = None):
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "repos": db.list_repos(),
                "message": MESSAGES.get(msg or ""),
                "running": scheduler.is_running(),
            },
        )

    @app.post("/repos")
    def add_repo(full_name: Annotated[str, Form()]):
        try:
            name = parse_repo_name(full_name)
        except ValueError:
            return _redirect("invalid")
        try:
            db.add_repo(name)
        except DuplicateRepoError:
            return _redirect("duplicate")
        return _redirect("added")

    @app.post("/repos/{repo_id}/delete")
    def delete_repo(repo_id: int):
        db.disable_repo(repo_id)
        return _redirect("removed")

    @app.post("/sync")
    def sync_now():
        return _redirect("sync_started" if scheduler.trigger_now() else "sync_busy")

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        recent = db.recent_runs(50)
        events = {run.id: db.events_for_run(run.id) for run in recent}
        return templates.TemplateResponse(
            request, "runs.html", {"runs": recent, "events": events}
        )

    @app.get("/health")
    def health():
        last = db.last_run()
        return {"ok": True, "last_run": last.finished_at if last else None}

    return app
```

- [ ] **Step 4: Create templates**

`app/templates/base.html`:

```html
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>github-bkp</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0 auto; max-width: 1000px; padding: 16px; color: #1f2328; }
    nav a { margin-right: 16px; }
    table { border-collapse: collapse; width: 100%; margin-top: 16px; }
    th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #d0d7de; font-size: 14px; }
    .badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; color: #fff; }
    .status-active { background: #1a7f37; }
    .status-pending { background: #6e7781; }
    .status-suspect { background: #bf8700; }
    .status-error { background: #cf222e; }
    .status-missing { background: #8250df; }
    .message { background: #ddf4ff; padding: 8px 12px; border-radius: 6px; }
    .muted { color: #656d76; font-size: 13px; }
    form.inline { display: inline; }
  </style>
</head>
<body>
  <nav><a href="/">Repositórios</a><a href="/runs">Execuções</a></nav>
  {% block content %}{% endblock %}
</body>
</html>
```

`app/templates/index.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Repositórios</h1>
{% if message %}<p class="message">{{ message }}</p>{% endif %}

<form method="post" action="/repos">
  <input name="full_name" placeholder="owner/repo ou https://github.com/owner/repo" size="50" required>
  <button type="submit">Adicionar</button>
</form>

<form method="post" action="/sync" style="margin-top: 8px">
  {% if running %}
    <button type="submit" disabled>Sincronização em execução…</button>
  {% else %}
    <button type="submit">Sincronizar agora</button>
  {% endif %}
</form>

<table>
  <thead><tr><th>Repositório</th><th>Status</th><th>Último sync ok</th><th>Tamanho</th><th>Último erro</th><th></th></tr></thead>
  <tbody>
  {% for r in repos %}
    <tr>
      <td><a href="https://github.com/{{ r.full_name }}">{{ r.full_name }}</a></td>
      <td><span class="badge status-{{ r.status.value }}">{{ r.status.value }}</span></td>
      <td>{{ r.last_success_at or "-" }}</td>
      <td>{{ r.size_bytes | size }}</td>
      <td class="muted">{{ r.last_error or "" }}</td>
      <td>
        <form class="inline" method="post" action="/repos/{{ r.id }}/delete"
              onsubmit="return confirm('Remover {{ r.full_name }} da lista? A cópia local é mantida.')">
          <button type="submit">Remover</button>
        </form>
      </td>
    </tr>
  {% else %}
    <tr><td colspan="6" class="muted">Nenhum repositório ainda.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
```

`app/templates/runs.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Execuções</h1>
<table>
  <thead><tr><th>#</th><th>Início</th><th>Fim</th><th>Origem</th><th>OK</th><th>Falhas</th></tr></thead>
  <tbody>
  {% for run in runs %}
    <tr>
      <td>{{ run.id }}</td>
      <td>{{ run.started_at }}</td>
      <td>{{ run.finished_at or "em execução" }}</td>
      <td>{{ run.trigger }}</td>
      <td>{{ run.ok_count }}</td>
      <td>{{ run.fail_count }}</td>
    </tr>
    {% if events[run.id] %}
    <tr><td></td><td colspan="5">
      <details><summary class="muted">{{ events[run.id] | length }} evento(s)</summary>
        <ul>
        {% for e in events[run.id] %}
          <li>{{ e.full_name or "-" }} — <strong>{{ e.kind }}</strong>{% if e.detail %}: {{ e.detail }}{% endif %}</li>
        {% endfor %}
        </ul>
      </details>
    </td></tr>
    {% endif %}
  {% else %}
    <tr><td colspan="6" class="muted">Nenhuma execução ainda.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_web.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add app/web.py app/templates tests/test_web.py
git commit -m "feat: web ui for managing repos and viewing runs"
```

---

### Task 12: Entry point (`app/main.py`)

**Files:**
- Create: `app/main.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_main.py`

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 3: Implement** — `app/main.py`

```python
import json
import logging
import shutil

import uvicorn
from fastapi import FastAPI

from app.config import Settings
from app.db import Database
from app.github_client import GitHubClient
from app.job import BackupJob
from app.notifier import Notifier
from app.scheduler import Scheduler
from app.web import create_app


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def build(settings: Settings) -> tuple[FastAPI, Scheduler]:
    if shutil.which("git") is None:
        raise RuntimeError("git não encontrado no PATH")
    db = Database(settings.db_path)
    token = settings.github_token.get_secret_value()
    job = BackupJob(
        db,
        GitHubClient(token, settings.github_api_url),
        Notifier(settings),
        repos_dir=settings.repos_dir,
        git_base_url=settings.github_git_url,
        token=token,
    )
    scheduler = Scheduler(
        job, sync_cron=settings.sync_cron, summary_cron=settings.summary_cron, tz=settings.tz
    )
    return create_app(db, scheduler), scheduler


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    settings = Settings()
    app, scheduler = build(settings)
    scheduler.start()
    try:
        uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_main.py -v`
Expected: 2 passed

- [ ] **Step 5: Run the whole suite + lint**

Run: `python -m pytest -q && ruff check .`
Expected: all tests pass; `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "feat: application entry point"
```

---

### Task 13: Docker, compose, env example, README

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Create `Dockerfile`**

```dockerfile
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app \
 && mkdir /data && chown app:app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

USER app
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status == 200 else 1)"

CMD ["github-bkp"]
```

- [ ] **Step 2: Create `.dockerignore`**

```
.git
.venv
.env
.archify
docs
tests
**/__pycache__
.pytest_cache
.ruff_cache
data
```

- [ ] **Step 3: Create `docker-compose.yml`**

```yaml
services:
  github-bkp:
    build: .
    image: github-bkp-system:latest
    container_name: github-bkp
    env_file: .env
    ports:
      # Web UI has no authentication: keep it on localhost or a LAN/VPN address only.
      - "127.0.0.1:8080:8080"
    volumes:
      - bkp-data:/data
    restart: unless-stopped

volumes:
  bkp-data:
```

- [ ] **Step 4: Create `.env.example`**

```dotenv
# GitHub fine-grained PAT: Repository access = the repos to back up,
# permissions Contents: Read-only + Metadata: Read-only.
GITHUB_TOKEN=github_pat_xxx

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_STARTTLS=true
SMTP_USER=you@gmail.com
# Gmail: use an App Password, not your account password.
SMTP_PASSWORD=app-password
MAIL_FROM=you@gmail.com
MAIL_TO=you@gmail.com

# Optional
SYNC_CRON=0 3 * * *
SUMMARY_CRON=0 9 * * 0
TZ=America/Sao_Paulo
```

- [ ] **Step 5: Write `README.md`**

````markdown
# github-bkp-system

Self-hosted service that keeps daily-updated mirrors of chosen GitHub repositories
and emails you when a repository disappears from GitHub (or comes back).

## What it does

- Keeps a bare `git` mirror (all branches and tags) of each repo under `/data/repos/<owner>/<repo>.git`.
- Updates every repo once a day (`SYNC_CRON`, default 03:00).
- Never loses history: refs that would vanish (force-push, deleted branch, moved tag) are copied to
  `refs/backup/<date>/...` before updating.
- Emails you when a repo returns 404 on two consecutive checks, when it comes back,
  when a backup fails 3 days in a row, plus a weekly summary.
- Simple web UI to add/remove repos, trigger a sync and see past runs.

## Setup

1. Create a fine-grained GitHub token with **Contents: Read-only** and **Metadata: Read-only**
   for the repos you want to back up.
2. `cp .env.example .env` and fill in the values.
3. Start it:

   ```bash
   docker compose up -d --build
   ```

4. Open http://127.0.0.1:8080 and add repos as `owner/repo` or a GitHub URL.

## Security

The web UI has **no authentication**. The compose file binds it to `127.0.0.1` only.
To reach it from other machines, change the port binding to a LAN or VPN address
(e.g. your Tailscale IP) — never expose it to the internet.

The GitHub token is passed to git through environment config on each command and is never
written to the mirrors' `config` files.

## Data

Everything lives in the `bkp-data` volume mounted at `/data`:

- `/data/state.db` — SQLite with repos, runs and events
- `/data/repos/<owner>/<repo>.git` — mirrors (never deleted by the service)

If you use a bind mount instead of the named volume, make the host directory writable by UID 1000.

## Restore

```bash
# working copy
git clone /data/repos/owner/repo.git repo
# republish to a new GitHub repo
git -C /data/repos/owner/repo.git push --mirror https://github.com/owner/new-repo.git
# list preserved refs
git -C /data/repos/owner/repo.git for-each-ref refs/backup
```

From the host, run these through the container:
`docker compose exec github-bkp git -C /data/repos/owner/repo.git for-each-ref refs/backup`.

## Development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest
ruff check .
```
````

- [ ] **Step 6: Build and smoke test the container**

```bash
cp .env.example .env    # dummy values are fine for the smoke test
docker compose up -d --build
docker compose ps        # wait until STATUS shows (healthy), up to ~1 min
curl -s http://127.0.0.1:8080/health
docker compose down
```

Expected: `{"ok":true,"last_run":null}` and container reported `healthy`.
(Delete the dummy `.env` afterwards if you don't want to keep it; it is git-ignored.)

- [ ] **Step 7: Commit**

```bash
git add Dockerfile .dockerignore docker-compose.yml .env.example README.md
git commit -m "feat: docker deployment and documentation"
```

---

### Task 14: Final verification and push

- [ ] **Step 1: Full test suite + lint**

Run: `python -m pytest -q && ruff check .`
Expected: all tests pass (≈ 100), `All checks passed!`

- [ ] **Step 2: End-to-end manual check (real GitHub)**

With a real `.env` (real token + SMTP):

```bash
docker compose up -d --build
```

1. Open http://127.0.0.1:8080, add one small public repo you own and one private repo.
2. Click "Sincronizar agora", reload after a few seconds: both `active`, size shown.
3. `docker compose exec github-bkp ls /data/repos/<owner>` shows both `.git` dirs.
4. Add `your-user/this-repo-does-not-exist`, sync twice: status goes `suspect` → `missing`, one email arrives.
5. `docker compose logs github-bkp` shows JSON log lines and no token.

- [ ] **Step 3: Push**

```bash
git push origin main
```
