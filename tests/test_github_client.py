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
