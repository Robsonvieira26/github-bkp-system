from pathlib import Path

from app.db import Event, Repo
from app.notifier import EmailMessage, Notifier, render_events_email, render_summary_email
from app.state import Status


def ev(
    kind,
    name="owner/repo",
    detail=None,
    last_success="2026-10-20T03:00:00+00:00",
    last_error=None,
    id=1,
):
    return Event(
        id=id,
        repo_id=1,
        run_id=1,
        kind=kind,
        detail=detail,
        created_at="2026-10-23T03:00:00+00:00",
        emailed=False,
        full_name=name,
        last_success_at=last_success,
        last_error=last_error,
    )


def repo(name, status=Status.ACTIVE, size=2048):
    return Repo(
        id=1,
        full_name=name,
        status=status,
        consecutive_404=0,
        consecutive_err=0,
        last_success_at="2026-10-23T03:00:00+00:00",
        last_checked_at=None,
        last_error=None,
        size_bytes=size,
        enabled=True,
        created_at="x",
    )


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
    n = Notifier(
        make_settings(smtp_port=465, smtp_starttls=False),
        smtp_factory=plain,
        smtp_ssl_factory=ssl_cls,
    )
    n.send(EmailMessage("Hi", "text", "<p>html</p>"))
    assert plain_created == []
    assert ssl_created[0].calls == [("login", "user", "pass")]
