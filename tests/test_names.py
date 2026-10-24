import pytest

from app.names import parse_description, parse_repo_name, parse_tags


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://github.com/mattpocock/skills/tree/main/skills/productivity/teach",
         "mattpocock/skills"),
        ("https://github.com/owner/repo/blob/main/README.md", "owner/repo"),
        ("https://github.com/owner/repo#readme", "owner/repo"),
        ("https://github.com/owner/repo?tab=readme", "owner/repo"),
    ],
)
def test_github_urls_with_extra_path(text, expected):
    assert parse_repo_name(text) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Edição, Estudos", ["Edição", "Estudos"]),
        (["  Vertex   AI ", "agentes", "Agentes"], ["Vertex AI", "agentes"]),
        ("", []),
        (" , ,", []),
        ([], []),
    ],
)
def test_parse_tags(value, expected):
    assert parse_tags(value) == expected


@pytest.mark.parametrize("value", ["x" * 41, ["a"] * 0 + [f"t{i}" for i in range(21)]])
def test_parse_tags_rejects(value):
    with pytest.raises(ValueError):
        parse_tags(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("  Edita vídeos  ", "Edita vídeos"), ("", None), ("   ", None), (None, None)],
)
def test_parse_description(value, expected):
    assert parse_description(value) == expected


def test_parse_description_too_long():
    with pytest.raises(ValueError):
        parse_description("x" * 501)
