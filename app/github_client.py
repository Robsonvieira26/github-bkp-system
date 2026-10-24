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
