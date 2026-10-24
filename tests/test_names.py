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
