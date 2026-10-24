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
