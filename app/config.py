from pathlib import Path

from pydantic import SecretStr, field_validator
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
    allowed_hosts: str = "127.0.0.1,localhost"

    @field_validator("mail_to")
    @classmethod
    def _require_recipient(cls, value: str) -> str:
        if not any(addr.strip() for addr in value.split(",")):
            raise ValueError("MAIL_TO precisa de pelo menos um destinatário")
        return value

    @property
    def mail_to_list(self) -> list[str]:
        return [addr.strip() for addr in self.mail_to.split(",") if addr.strip()]

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"
