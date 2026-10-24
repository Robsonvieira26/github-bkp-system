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
