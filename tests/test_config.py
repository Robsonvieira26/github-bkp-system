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
